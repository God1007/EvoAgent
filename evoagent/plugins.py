"""Trusted in-process plugin runtime for EvoAgent application capabilities.

Dynamic review skills remain isolated by :mod:`evoagent.skills`.  This module
serves a different trust boundary: operators use it to compose trusted storage,
queue, model, workflow, delivery, and observability providers.
"""

from __future__ import annotations

import importlib.metadata
import re
import threading
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, Protocol, TypeVar, cast, runtime_checkable

PLUGIN_API_VERSION = "evoagent.plugin/v1"
PLUGIN_ENTRYPOINT_GROUP = "evoagent.plugins"

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")

T = TypeVar("T")
EventHandler = Callable[["RuntimeEvent"], None]
Cleanup = Callable[[], None]


class PluginError(RuntimeError):
    """Base error for plugin discovery, validation, and lifecycle failures."""


class PluginConfigurationError(PluginError):
    """The selected plugin graph or profile is invalid."""


class PluginActivationError(PluginError):
    """A plugin failed to start and the candidate runtime was rolled back."""


class PluginShutdownError(PluginError):
    """One or more plugin cleanup callbacks failed during shutdown."""


class CapabilityNotFoundError(PluginError, LookupError):
    """No provider is registered for a required capability."""


@dataclass(frozen=True)
class CapabilityKey(Generic[T]):
    """A stable, typed name used by providers and consumers."""

    name: str
    multiple: bool = False

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name):
            raise ValueError("invalid capability name: %s" % self.name)


@dataclass(frozen=True)
class PluginManifest:
    """Declarative contract used before plugin code is activated."""

    plugin_id: str
    version: str
    provides: tuple[str, ...]
    requires: tuple[str, ...] = ()
    optional_requires: tuple[str, ...] = ()
    api_version: str = PLUGIN_API_VERSION
    priority: int = 0
    description: str = ""

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.plugin_id):
            raise ValueError("invalid plugin id: %s" % self.plugin_id)
        if not _VERSION.fullmatch(self.version):
            raise ValueError(
                "plugin %s has invalid semantic version: %s" % (self.plugin_id, self.version)
            )
        if self.api_version != PLUGIN_API_VERSION:
            raise ValueError(
                "plugin %s targets unsupported API %s (expected %s)"
                % (self.plugin_id, self.api_version, PLUGIN_API_VERSION)
            )
        for group_name, names in (
            ("provides", self.provides),
            ("requires", self.requires),
            ("optional_requires", self.optional_requires),
        ):
            if len(names) != len(set(names)):
                raise ValueError("plugin %s has duplicate %s" % (self.plugin_id, group_name))
            for name in names:
                if not _IDENTIFIER.fullmatch(name):
                    raise ValueError(
                        "plugin %s declares invalid capability: %s" % (self.plugin_id, name)
                    )
        overlap = set(self.provides).intersection(set(self.requires).union(self.optional_requires))
        if overlap:
            raise ValueError(
                "plugin %s both provides and requires: %s"
                % (self.plugin_id, ", ".join(sorted(overlap)))
            )


@runtime_checkable
class Plugin(Protocol):
    manifest: PluginManifest

    def start(self, context: PluginContext) -> Cleanup | None:
        """Register capabilities/effects and optionally return a cleanup callback."""


@dataclass(frozen=True)
class PluginProfile:
    """Operator-selected plugin set and per-plugin configuration."""

    name: str = "default"
    enabled: frozenset[str] = frozenset()
    disabled: frozenset[str] = frozenset()
    plugin_config: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("plugin profile name must not be empty")
        overlap = self.enabled.intersection(self.disabled)
        if overlap:
            raise ValueError(
                "plugins cannot be both enabled and disabled: %s" % ", ".join(sorted(overlap))
            )
        for plugin_id in self.enabled.union(self.disabled).union(self.plugin_config):
            if not _IDENTIFIER.fullmatch(plugin_id):
                raise ValueError("invalid plugin id in profile: %s" % plugin_id)

    def selects(self, plugin_id: str) -> bool:
        if plugin_id in self.disabled:
            return False
        return not self.enabled or plugin_id in self.enabled

    def config_for(self, plugin_id: str) -> Mapping[str, Any]:
        return MappingProxyType(dict(self.plugin_config.get(plugin_id, {})))

    @classmethod
    def from_toml(cls, path: str | Path) -> PluginProfile:
        source = Path(path)
        try:
            with source.open("rb") as handle:
                document = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise PluginConfigurationError(
                "cannot load plugin profile %s: %s" % (source, exc)
            ) from exc

        profile = document.get("profile", {})
        plugins = document.get("plugins", {})
        if not isinstance(profile, dict) or not isinstance(plugins, dict):
            raise PluginConfigurationError("profile and plugins must be TOML tables")
        enabled = _string_set(profile.get("enabled", []), "profile.enabled")
        disabled = _string_set(profile.get("disabled", []), "profile.disabled")
        config: dict[str, Mapping[str, Any]] = {}
        for plugin_id, value in plugins.items():
            if not isinstance(value, dict):
                raise PluginConfigurationError("plugins.%s must be a TOML table" % plugin_id)
            plugin_enabled = value.get("enabled")
            if plugin_enabled is not None and not isinstance(plugin_enabled, bool):
                raise PluginConfigurationError("plugins.%s.enabled must be a boolean" % plugin_id)
            if plugin_enabled is True and enabled:
                enabled.add(plugin_id)
            elif plugin_enabled is False:
                disabled.add(plugin_id)
            raw_config = value.get("config", {})
            if not isinstance(raw_config, dict):
                raise PluginConfigurationError("plugins.%s.config must be a TOML table" % plugin_id)
            config[plugin_id] = raw_config
        try:
            return cls(
                name=str(profile.get("name", source.stem)),
                enabled=frozenset(enabled),
                disabled=frozenset(disabled),
                plugin_config=MappingProxyType(config),
            )
        except ValueError as exc:
            raise PluginConfigurationError(str(exc)) from exc


def _string_set(value: Any, field_name: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PluginConfigurationError("%s must be an array of strings" % field_name)
    return set(value)


@dataclass(frozen=True)
class RuntimeEvent:
    name: str
    payload: Mapping[str, Any]
    scope: str = "global"

    def __post_init__(self) -> None:
        if not _EVENT_NAME.fullmatch(self.name):
            raise ValueError("invalid event name: %s" % self.name)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class EventFailure:
    plugin_id: str
    error: str


@dataclass
class _Subscription:
    event_name: str
    plugin_id: str
    handler: EventHandler
    priority: int
    sequence: int


class EventBus:
    """Thread-safe observer bus; listener failures are isolated and reported."""

    def __init__(self):
        self._lock = threading.RLock()
        self._subscriptions: dict[str, list[_Subscription]] = {}
        self._sequence = 0

    def subscribe(
        self,
        event_name: str,
        plugin_id: str,
        handler: EventHandler,
        priority: int = 0,
    ) -> Cleanup:
        if event_name != "*" and not _EVENT_NAME.fullmatch(event_name):
            raise ValueError("invalid event name: %s" % event_name)
        with self._lock:
            self._sequence += 1
            subscription = _Subscription(event_name, plugin_id, handler, priority, self._sequence)
            self._subscriptions.setdefault(event_name, []).append(subscription)

        def dispose() -> None:
            with self._lock:
                values = self._subscriptions.get(event_name, [])
                if subscription in values:
                    values.remove(subscription)
                if not values:
                    self._subscriptions.pop(event_name, None)

        return _once(dispose)

    def publish(self, event: RuntimeEvent) -> list[EventFailure]:
        with self._lock:
            listeners = list(self._subscriptions.get(event.name, []))
            listeners.extend(self._subscriptions.get("*", []))
        listeners.sort(key=lambda item: (-item.priority, item.sequence))
        failures = []
        for listener in listeners:
            try:
                listener.handler(event)
            except Exception as exc:  # observer failures must not break the review path
                failures.append(EventFailure(listener.plugin_id, str(exc)[:1000]))
        return failures


@dataclass
class _Provider:
    capability: str
    value: Any
    plugin_id: str
    priority: int
    sequence: int


class CapabilityRegistry:
    """Layered capability repository with nearest-scope shadowing."""

    def __init__(self, parent: CapabilityRegistry | None = None):
        self.parent = parent
        self._lock = threading.RLock()
        self._providers: dict[str, list[_Provider]] = {}
        self._sequence = 0

    def register(
        self,
        key: CapabilityKey[T],
        value: T,
        plugin_id: str,
        priority: int = 0,
    ) -> Cleanup:
        with self._lock:
            self._sequence += 1
            provider = _Provider(key.name, value, plugin_id, priority, self._sequence)
            self._providers.setdefault(key.name, []).append(provider)

        def dispose() -> None:
            with self._lock:
                values = self._providers.get(key.name, [])
                if provider in values:
                    values.remove(provider)
                if not values:
                    self._providers.pop(key.name, None)

        return _once(dispose)

    def get(self, key: CapabilityKey[T], default: T | None = None) -> T | None:
        provider = self._selected(key.name)
        if provider is not None:
            return cast(T, provider.value)
        if self.parent is not None:
            return self.parent.get(key, default)
        return default

    def require(self, key: CapabilityKey[T]) -> T:
        value = self.get(key)
        if value is None:
            raise CapabilityNotFoundError("required capability is unavailable: %s" % key.name)
        return value

    def all(self, key: CapabilityKey[T]) -> list[T]:
        with self._lock:
            local = sorted(
                self._providers.get(key.name, []),
                key=lambda item: (-item.priority, -item.sequence),
            )
        values = [cast(T, item.value) for item in local]
        if self.parent is not None:
            values.extend(self.parent.all(key))
        return values

    def has(self, capability: str) -> bool:
        with self._lock:
            if self._providers.get(capability):
                return True
        return self.parent.has(capability) if self.parent is not None else False

    def has_provider(self, capability: str, plugin_id: str) -> bool:
        with self._lock:
            return any(item.plugin_id == plugin_id for item in self._providers.get(capability, []))

    def inventory(self) -> dict[str, list[str]]:
        inherited = self.parent.inventory() if self.parent is not None else {}
        with self._lock:
            for capability, providers in self._providers.items():
                inherited[capability] = [
                    item.plugin_id
                    for item in sorted(providers, key=lambda item: (-item.priority, -item.sequence))
                ]
        return inherited

    def _selected(self, capability: str) -> _Provider | None:
        with self._lock:
            providers = self._providers.get(capability, [])
            if not providers:
                return None
            return max(providers, key=lambda item: (item.priority, item.sequence))


class PluginContext:
    """Capability/event facade limited to a plugin's declared contract."""

    def __init__(
        self,
        capabilities: CapabilityRegistry,
        events: EventBus,
        manifest: PluginManifest,
        config: Mapping[str, Any],
        scope: str,
    ):
        self._capabilities = capabilities
        self._events = events
        self.manifest = manifest
        self.config = config
        self.scope = scope
        self._effects: list[Cleanup] = []
        self._disposed = False

    def provide(self, key: CapabilityKey[T], value: T, priority: int | None = None) -> None:
        if key.name not in self.manifest.provides:
            raise PluginConfigurationError(
                "plugin %s registered undeclared capability %s"
                % (self.manifest.plugin_id, key.name)
            )
        self._effects.append(
            self._capabilities.register(
                key,
                value,
                self.manifest.plugin_id,
                self.manifest.priority if priority is None else priority,
            )
        )

    def require(self, key: CapabilityKey[T]) -> T:
        declared = set(self.manifest.requires).union(self.manifest.optional_requires)
        if key.name not in declared:
            raise PluginConfigurationError(
                "plugin %s consumed undeclared capability %s" % (self.manifest.plugin_id, key.name)
            )
        return self._capabilities.require(key)

    def optional(self, key: CapabilityKey[T]) -> T | None:
        if key.name not in self.manifest.optional_requires:
            raise PluginConfigurationError(
                "plugin %s consumed undeclared optional capability %s"
                % (self.manifest.plugin_id, key.name)
            )
        return self._capabilities.get(key)

    def all(self, key: CapabilityKey[T]) -> list[T]:
        declared = set(self.manifest.requires).union(self.manifest.optional_requires)
        if key.name not in declared:
            raise PluginConfigurationError(
                "plugin %s consumed undeclared capability collection %s"
                % (self.manifest.plugin_id, key.name)
            )
        if not key.multiple:
            raise PluginConfigurationError("capability %s is not multi-valued" % key.name)
        return self._capabilities.all(key)

    def subscribe(self, event_name: str, handler: EventHandler, priority: int = 0) -> None:
        self._effects.append(
            self._events.subscribe(event_name, self.manifest.plugin_id, handler, priority)
        )

    def defer(self, cleanup: Cleanup) -> None:
        if not callable(cleanup):
            raise TypeError("plugin cleanup must be callable")
        self._effects.append(_once(cleanup))

    def dispose(self) -> list[Exception]:
        if self._disposed:
            return []
        self._disposed = True
        errors = []
        for effect in reversed(self._effects):
            try:
                effect()
            except Exception as exc:  # cleanup is best-effort but fully drained
                errors.append(exc)
        self._effects.clear()
        return errors


class RuntimeState(str, Enum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class PluginRuntime:
    """Dependency-aware, transactional runtime for a selected plugin graph."""

    def __init__(
        self,
        plugins: Sequence[Plugin],
        profile: PluginProfile | None = None,
        *,
        parent: PluginRuntime | None = None,
        scope: str = "global",
    ):
        self.profile = profile or PluginProfile()
        self.scope = scope
        self.parent = parent
        self.events: EventBus = parent.events if parent is not None else EventBus()
        self.capabilities: CapabilityRegistry = CapabilityRegistry(
            parent.capabilities if parent is not None else None
        )
        self._plugins = self._select_plugins(plugins)
        self._contexts: list[PluginContext] = []
        self._activation_order: list[str] = []
        self._children: list[PluginRuntime] = []
        self._lock = threading.RLock()
        self.state = RuntimeState.NEW

    def start(self) -> PluginRuntime:
        if self.parent is not None:
            with self.parent._lock:
                if self.parent.state != RuntimeState.RUNNING:
                    raise PluginActivationError("parent runtime is not running")
        with self._lock:
            if self.state == RuntimeState.RUNNING:
                return self
            if self.state != RuntimeState.NEW:
                raise PluginActivationError("runtime cannot start from state %s" % self.state.value)
            self.state = RuntimeState.STARTING
            try:
                for plugin in self._ordered_plugins():
                    context = PluginContext(
                        self.capabilities,
                        self.events,
                        plugin.manifest,
                        self.profile.config_for(plugin.manifest.plugin_id),
                        self.scope,
                    )
                    self._contexts.append(context)
                    cleanup = plugin.start(context)
                    if cleanup is not None:
                        context.defer(cleanup)
                    missing = [
                        capability
                        for capability in plugin.manifest.provides
                        if not self.capabilities.has_provider(capability, plugin.manifest.plugin_id)
                    ]
                    if missing:
                        raise PluginConfigurationError(
                            "plugin %s did not register declared capabilities: %s"
                            % (plugin.manifest.plugin_id, ", ".join(missing))
                        )
                    self._activation_order.append(plugin.manifest.plugin_id)
            except Exception as exc:
                self._rollback()
                self.state = RuntimeState.FAILED
                if isinstance(exc, PluginConfigurationError):
                    raise
                raise PluginActivationError("plugin runtime activation failed: %s" % exc) from exc
            self.state = RuntimeState.RUNNING
        return self

    def stop(self) -> None:
        with self._lock:
            if self.state == RuntimeState.STOPPING:
                return
            if self.state in {RuntimeState.NEW, RuntimeState.STOPPED}:
                self.state = RuntimeState.STOPPED
                errors: list[Exception] = []
            else:
                self.state = RuntimeState.STOPPING
                errors = []
                for child in reversed(list(self._children)):
                    try:
                        child.stop()
                    except Exception as exc:
                        errors.append(exc)
                self._children.clear()
                errors.extend(self._rollback())
                self.state = RuntimeState.STOPPED
        if self.parent is not None:
            self.parent._detach_child(self)
        if errors:
            raise PluginShutdownError(
                "%d plugin cleanup callback(s) failed; first error: %s" % (len(errors), errors[0])
            )

    def create_scope(
        self,
        scope: str,
        plugins: Sequence[Plugin],
        profile: PluginProfile | None = None,
    ) -> PluginRuntime:
        if not scope.strip() or scope == "global":
            raise ValueError("child scope must have a non-global name")
        with self._lock:
            if self.state != RuntimeState.RUNNING:
                raise PluginConfigurationError(
                    "parent runtime must be running before creating a scope"
                )
            child = PluginRuntime(plugins, profile, parent=self, scope=scope)
            self._children.append(child)
        return child

    def _detach_child(self, child: PluginRuntime) -> None:
        with self._lock:
            if child in self._children:
                self._children.remove(child)

    def require(self, key: CapabilityKey[T]) -> T:
        with self._lock:
            if self.state != RuntimeState.RUNNING:
                raise PluginConfigurationError(
                    "runtime capabilities are unavailable before startup"
                )
            return self.capabilities.require(key)

    def publish(self, name: str, payload: Mapping[str, Any]) -> list[EventFailure]:
        return self.events.publish(RuntimeEvent(name, payload, self.scope))

    def describe(self) -> dict[str, Any]:
        with self._lock:
            details = []
            for plugin_id in self._activation_order:
                manifest = self._plugins[plugin_id].manifest
                details.append(
                    {
                        "id": manifest.plugin_id,
                        "version": manifest.version,
                        "api_version": manifest.api_version,
                        "provides": list(manifest.provides),
                        "requires": list(manifest.requires),
                        "optional_requires": list(manifest.optional_requires),
                        "priority": manifest.priority,
                        "description": manifest.description,
                    }
                )
            return {
                "state": self.state.value,
                "scope": self.scope,
                "profile": self.profile.name,
                "plugins": list(self._activation_order),
                "plugin_details": details,
                "capabilities": self.capabilities.inventory(),
                "child_scopes": [child.scope for child in self._children],
            }

    def _select_plugins(self, plugins: Sequence[Plugin]) -> dict[str, Plugin]:
        selected: dict[str, Plugin] = {}
        available: set[str] = set()
        for plugin in plugins:
            if not isinstance(plugin, Plugin):
                raise PluginConfigurationError(
                    "plugin object does not implement the Plugin protocol"
                )
            plugin_id = plugin.manifest.plugin_id
            if plugin_id in available:
                raise PluginConfigurationError("duplicate plugin id: %s" % plugin_id)
            available.add(plugin_id)
            if self.profile.selects(plugin_id):
                selected[plugin_id] = plugin
        referenced = (
            set(self.profile.enabled).union(self.profile.disabled).union(self.profile.plugin_config)
        )
        unknown = referenced.difference(available)
        if unknown:
            raise PluginConfigurationError(
                "profile references unavailable plugins: %s" % ", ".join(sorted(unknown))
            )
        return selected

    def _ordered_plugins(self) -> list[Plugin]:
        providers: dict[str, list[Plugin]] = {}
        for plugin in self._plugins.values():
            for capability in plugin.manifest.provides:
                providers.setdefault(capability, []).append(plugin)
        for values in providers.values():
            values.sort(key=lambda item: (-item.manifest.priority, item.manifest.plugin_id))

        dependencies: dict[str, set[str]] = {plugin_id: set() for plugin_id in self._plugins}
        missing: dict[str, list[str]] = {}
        for plugin_id, plugin in self._plugins.items():
            for capability in plugin.manifest.requires:
                if self.capabilities.has(capability):
                    continue
                candidates = providers.get(capability, [])
                if not candidates:
                    missing.setdefault(plugin_id, []).append(capability)
                    continue
                dependencies[plugin_id].update(
                    candidate.manifest.plugin_id for candidate in candidates
                )
            for capability in plugin.manifest.optional_requires:
                if self.capabilities.has(capability):
                    continue
                candidates = providers.get(capability, [])
                if candidates:
                    dependencies[plugin_id].update(
                        candidate.manifest.plugin_id for candidate in candidates
                    )
        if missing:
            detail = "; ".join(
                "%s requires %s" % (plugin_id, ", ".join(sorted(capabilities)))
                for plugin_id, capabilities in sorted(missing.items())
            )
            raise PluginConfigurationError("unsatisfied plugin dependencies: %s" % detail)

        ordered: list[Plugin] = []
        remaining = {plugin_id: set(values) for plugin_id, values in dependencies.items()}
        while remaining:
            ready = [
                self._plugins[plugin_id]
                for plugin_id, required in remaining.items()
                if not required
            ]
            if not ready:
                cycle = ", ".join(sorted(remaining))
                raise PluginConfigurationError("plugin dependency cycle detected: %s" % cycle)
            ready.sort(key=lambda item: (-item.manifest.priority, item.manifest.plugin_id))
            for plugin in ready:
                plugin_id = plugin.manifest.plugin_id
                ordered.append(plugin)
                remaining.pop(plugin_id)
                for required in remaining.values():
                    required.discard(plugin_id)
        return ordered

    def _rollback(self) -> list[Exception]:
        errors = []
        for context in reversed(self._contexts):
            errors.extend(context.dispose())
        self._contexts.clear()
        self._activation_order.clear()
        return errors


class ProviderPlugin(Generic[T]):
    """Small adapter for the common one-plugin/one-capability case."""

    def __init__(
        self,
        manifest: PluginManifest,
        capability: CapabilityKey[T],
        factory: Callable[[PluginContext], T],
        *,
        close: Callable[[T], None] | None = None,
    ):
        if manifest.provides != (capability.name,):
            raise ValueError("provider manifest must declare exactly %s" % capability.name)
        self.manifest = manifest
        self.capability = capability
        self.factory = factory
        self.close = close

    def start(self, context: PluginContext) -> None:
        value = self.factory(context)
        context.provide(self.capability, value)
        close = self.close
        if close is not None:
            context.defer(lambda: close(value))


def discover_plugins(allowlist: Iterable[str]) -> list[Plugin]:
    """Load explicitly allowed trusted plugins from Python entry points.

    Entry-point plugins run in-process and are therefore never discovered
    implicitly.  Untrusted review logic belongs in the sandboxed Skill system.
    """

    allowed = set(allowlist)
    if not allowed:
        return []
    available = importlib.metadata.entry_points()
    entries = available.select(group=PLUGIN_ENTRYPOINT_GROUP)
    found: dict[str, Plugin] = {}
    for entry in entries:
        if entry.name not in allowed:
            continue
        try:
            loaded = entry.load()
            candidate = loaded() if callable(loaded) and not isinstance(loaded, Plugin) else loaded
        except Exception as exc:
            raise PluginConfigurationError(
                "failed to load trusted plugin entry point %s: %s" % (entry.name, exc)
            ) from exc
        if not isinstance(candidate, Plugin):
            raise PluginConfigurationError(
                "entry point %s did not produce an EvoAgent plugin" % entry.name
            )
        if candidate.manifest.plugin_id != entry.name:
            raise PluginConfigurationError(
                "entry point %s produced mismatched plugin id %s"
                % (entry.name, candidate.manifest.plugin_id)
            )
        found[entry.name] = candidate
    missing = allowed.difference(found)
    if missing:
        raise PluginConfigurationError(
            "trusted plugins are not installed: %s" % ", ".join(sorted(missing))
        )
    return list(found.values())


def _once(callback: Cleanup) -> Cleanup:
    lock = threading.Lock()
    called = False

    def wrapper() -> None:
        nonlocal called
        with lock:
            if called:
                return
            called = True
        callback()

    return wrapper
