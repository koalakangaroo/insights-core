"""
This module implements dependency resolution and execution within Red Hat Insights.
"""

import inspect
import logging
import os
import pkgutil
import re
import six
import sys
import time
import traceback

from collections import defaultdict
from functools import reduce as _reduce
from insights.contrib import importlib
from insights.contrib.toposort import toposort_flatten
from insights.util import defaults, enum, KeyPassingDefaultDict

log = logging.getLogger(__name__)

GROUPS = enum("single", "cluster")

MODULE_NAMES = {}
BASE_MODULE_NAMES = {}

TYPE_OBSERVERS = defaultdict(set)

ALIASES_BY_COMPONENT = {}
ALIASES = {}
COMPONENTS_BY_TYPE = defaultdict(set)
DEPENDENCIES = defaultdict(set)
DEPENDENTS = defaultdict(set)
COMPONENTS = defaultdict(lambda: defaultdict(set))

DELEGATES = {}
HIDDEN = set()
IGNORE = defaultdict(set)

ANY_TYPE = object()


def get_delegate(component):
    return DELEGATES.get(component)


def add_ignore(c, i):
    IGNORE[c].add(i)


def _get_from_module(name):
    mod, _, n = name.rpartition(".")
    if mod not in sys.modules:
        importlib.import_module(mod)
    return getattr(sys.modules[mod], n)


def _get_from_class(name):
    mod, _, n = name.rpartition(".")
    cls = _get_from_module(mod)
    return getattr(cls, n)


def _get_component(name):
    """ Returns a class, function, or class method specified by the fully
        qualified name.
    """
    for f in (_get_from_module, _get_from_class):
        try:
            return f(name)
        except:
            pass
    log.debug("Couldn't load %s" % name)


COMPONENT_NAME_CACHE = KeyPassingDefaultDict(_get_component)
get_component = COMPONENT_NAME_CACHE.__getitem__


@defaults(None)
def get_component_type(component):
    return get_delegate(component).type


@defaults(None)
def get_group(component):
    return get_delegate(component).group


def add_dependent(component, dep):
    DEPENDENTS[component].add(dep)


def get_dependents(component):
    return DEPENDENTS.get(component, set())


@defaults(set())
def get_dependencies(component):
    return get_delegate(component).get_dependencies()


def add_dependency(component, dep):
    get_delegate(component).add_dependency(dep)


@defaults([])
def get_added_dependencies(component):
    return get_delegate(component).added_dependencies


def add_observer(o, component_type=ANY_TYPE):
    TYPE_OBSERVERS[component_type].add(o)


def observer(component_type=ANY_TYPE):
    def inner(func):
        add_observer(func, component_type)
        return func
    return inner


class MissingRequirements(Exception):
    def __init__(self, requirements):
        self.requirements = requirements
        super(MissingRequirements, self).__init__(requirements)


class SkipComponent(Exception):
    """ This class should be raised by components that want to be taken out of
        dependency resolution.
    """
    pass


def get_name(component):
    if six.callable(component):
        name = getattr(component, "__qualname__", component.__name__)
        return '.'.join([component.__module__, name])
    return str(component)


def get_simple_name(component):
    if six.callable(component):
        return component.__name__
    return str(component)


def get_metadata(component):
    return get_delegate(component).metadata if component in DELEGATES else {}


def get_module_name(obj):
    try:
        return inspect.getmodule(obj).__name__
    except:
        return None


def get_base_module_name(obj):
    try:
        return get_module_name(obj).split(".")[-1]
    except:
        return None


def mark_hidden(component):
    global HIDDEN
    if isinstance(component, (list, set)):
        HIDDEN |= set(component)
    else:
        HIDDEN.add(component)


def is_hidden(component):
    return component in HIDDEN


def walk_dependencies(root, visitor):
    """ Call visitor on root and all dependencies reachable from it in breadth
        first order.

        :param component root: component function or class
        :param function visitor: signature is `func(component, parent)`.
            The call on root is `visitor(root, None)`.
    """
    def visit(parent, visitor):
        for d in get_dependencies(parent):
            visitor(d, parent)
            visit(d, visitor)

    visitor(root, None)
    visit(root, visitor)


def get_dependency_graph(component):
    if component not in DEPENDENCIES:
        raise Exception("%s is not a registered component." % get_name(component))

    if not DEPENDENCIES[component]:
        return {component: set()}

    graph = defaultdict(set)

    def visitor(c, parent):
        if parent is not None:
            graph[parent].add(c)

    walk_dependencies(component, visitor)

    graph = dict(graph)

    # Find all items that don't depend on anything.
    extra_items_in_deps = _reduce(set.union, graph.values(), set()) - set(graph.keys())

    # Add empty dependences where needed.
    graph.update(dict((item, set()) for item in extra_items_in_deps))

    return graph


def get_subgraphs(graph=DEPENDENCIES):
    keys = set(graph)
    frontier = set()
    seen = set()
    while keys:
        frontier.add(keys.pop())
        while frontier:
            component = frontier.pop()
            seen.add(component)
            frontier |= set([d for d in get_dependencies(component) if d in graph])
            frontier |= set([d for d in get_dependents(component) if d in graph])
            frontier -= seen
        yield dict((s, get_dependencies(s)) for s in seen)
        keys -= seen
        seen.clear()


def _import(path, continue_on_error):
    log.debug("Importing %s" % path)
    try:
        return importlib.import_module(path)
    except Exception as ex:
        log.exception(ex)
        if not continue_on_error:
            raise


def load_components(path, include=".*", exclude="test", continue_on_error=True):
    num_loaded = 0
    if path.endswith(".py"):
        path, _ = os.path.splitext(path)

    path = path.rstrip("/").replace("/", ".")

    package = _import(path, continue_on_error)
    if not package:
        return 0

    num_loaded += 1

    do_include = re.compile(include).search if include else lambda x: True
    do_exclude = re.compile(exclude).search if exclude else lambda x: False

    if not hasattr(package, "__path__"):
        return num_loaded

    prefix = package.__name__ + "."
    for _, name, is_pkg in pkgutil.iter_modules(path=package.__path__, prefix=prefix):
        if not name.startswith(prefix):
            name = prefix + name
        if is_pkg:
            num_loaded += load_components(name, include, exclude, continue_on_error)
        else:
            if do_include(name) and not do_exclude(name):
                _import(name, continue_on_error)
                num_loaded += 1

    return num_loaded


def first_of(dependencies, broker):
    for d in dependencies:
        if d in broker:
            return broker[d]


def split_requirements(requires):
    req_all = []
    req_any = []
    for r in requires:
        if isinstance(r, list):
            req_any.append(r)
        else:
            req_all.append(r)
    return req_all, req_any


def stringify_requirements(requires):
    if isinstance(requires, tuple):
        req_all, req_any = requires
    else:
        req_all, req_any = split_requirements(requires)
    pretty_all = [get_name(r) for r in req_all]
    pretty_any = [str([get_name(r) for r in any_list]) for any_list in req_any]
    result = "All: %s" % pretty_all + " Any: " + " Any: ".join(pretty_any)
    return result


def register_component(delegate):
    component = delegate.component

    dependencies = delegate.get_dependencies()
    DEPENDENCIES[component] = dependencies
    COMPONENTS[delegate.group][component] |= dependencies

    COMPONENTS_BY_TYPE[delegate.type].add(component)
    DELEGATES[component] = delegate

    MODULE_NAMES[component] = get_module_name(component)
    BASE_MODULE_NAMES[component] = get_base_module_name(component)

    name = get_name(component)
    COMPONENT_NAME_CACHE[name] = component


class Broker(object):
    def __init__(self, seed_broker=None):
        self.instances = dict(seed_broker.instances) if seed_broker else {}
        self.missing_requirements = {}
        self.exceptions = defaultdict(list)
        self.tracebacks = {}
        self.exec_times = {}

        self.observers = defaultdict(set)
        self.observers[ANY_TYPE] = set()
        for k, v in TYPE_OBSERVERS.items():
            self.observers[k] = set(v)

    def observer(self, component_type=ANY_TYPE):
        def inner(func):
            self.add_observer(func, component_type)
            return func
        return inner

    def add_observer(self, o, component_type=ANY_TYPE):
        self.observers[component_type].add(o)

    def fire_observers(self, component):
        _type = get_component_type(component)
        if not _type:
            return

        for o in self.observers.get(_type, set()) | self.observers[ANY_TYPE]:
            try:
                o(component, self)
            except Exception as e:
                log.exception(e)

    def add_exception(self, component, ex, tb=None):
        if isinstance(ex, MissingRequirements):
            self.missing_requirements[component] = ex.requirements
        else:
            self.exceptions[component].append(ex)
            self.tracebacks[ex] = tb

    def keys(self):
        return self.instances.keys()

    def items(self):
        return self.instances.items()

    def get_by_type(self, _type):
        r = {}
        for k, v in self.items():
            if get_component_type(k) is _type:
                r[k] = v
        return r

    def __contains__(self, component):
        return component in self.instances

    def __setitem__(self, component, instance):
        msg = "Already exists in broker with key: %s"
        if component in self.instances:
            raise KeyError(msg % get_name(component))

        self.instances[component] = instance

    def __delitem__(self, component):
        if component in self.instances:
            del self.instances[component]
            return

    def __getitem__(self, component):
        if component in self.instances:
            return self.instances[component]

        raise KeyError("Unknown component: %s" % get_name(component))

    def get(self, component, default=None):
        try:
            return self[component]
        except KeyError:
            return default


def get_missing_requirements(func, requires, d):
    if not requires:
        return None
    if any(i in d for i in IGNORE.get(func, [])):
        raise SkipComponent()
    req_all, req_any = split_requirements(requires)
    d = set(d.keys())
    req_all = [r for r in req_all if r not in d]
    req_any = [r for r in req_any if set(r).isdisjoint(d)]
    if req_all or req_any:
        return req_all, req_any
    else:
        return None


def broker_executor(func, broker, requires=[], optional=[]):
    missing_requirements = get_missing_requirements(func, requires, broker)
    if missing_requirements:
        raise MissingRequirements(missing_requirements)
    return func(broker)


def default_executor(func, broker, requires=[], optional=[]):
    """ Use this executor if your component signature matches your
        dependency list. Can be used on individual components or
        in component type definitions.
    """
    missing_requirements = get_missing_requirements(func, requires, broker)
    if missing_requirements:
        raise MissingRequirements(missing_requirements)
    args = []
    for r in requires:
        if isinstance(r, list):
            args.extend(r)
        else:
            args.append(r)
    args.extend(optional)
    args = [broker.get(a) for a in args]
    return func(*args)


class Delegate(object):
    def __init__(self, component, requires, optional):
        self.__name__ = component.__name__
        self.__module__ = component.__module__
        self.__doc__ = component.__doc__
        self.__qualname__ = getattr(component, "__qualname__", None)

        self.component = component
        self.executor = default_executor
        self.group = None
        self.metadata = {}
        self.requires = requires
        self.optional = optional
        self.added_dependencies = []
        self.type = None

        if requires:
            _all, _any = split_requirements(requires)
            _all = set(_all)
            _any = set(i for o in _any for i in o)
        else:
            _all, _any = set(), set()
        _optional = set(optional) if optional else set()

        self.dependencies = _all | _any | _optional
        for d in self.dependencies:
            add_dependent(d, component)

    def get_dependencies(self):
        return self.dependencies

    def add_dependency(self, dep):
        group = self.group
        self.added_dependencies.append(dep)
        self.dependencies.add(dep)
        add_dependent(dep, self.component)

        DEPENDENCIES[self.component].add(dep)
        COMPONENTS[group][self.component].add(dep)

    def __call__(self, broker):
        return self.executor(self.component, broker, self.requires, self.optional)


def new_component_type(name=None,
                       auto_requires=[],
                       auto_optional=[],
                       group=GROUPS.single,
                       executor=default_executor,
                       type_metadata={},
                       delegate_class=Delegate):
    """ Factory that creates component decorators.

        The functions this factory produces are decorators for parsers, combiners,
        rules, cluster rules, etc.

        Args:
            name (str): the name of the component type the produced decorator
                will define
            auto_requires (list): All decorated components automatically have
                this requires spec. Anything specified when decorating a component
                is added to this spec.
            auto_optional (list): All decorated components automatically have
                this optional spec. Anything specified when decorating a component
                is added to this spec.
            group (type): any symbol to group this component with similar components
                in the dependency list. This will be used when calling run to
                select the set of components to be executed: run(COMPONENTS[group])
            executor (func): an optional function that controls how a component is
                executed. It can impose restrictions on return value types, perform
                component type specific exception handling, etc. The signature is
                `executor(component, broker, requires=?, optional=?)`.
                The default behavior is to call `default_executor`.
            type_metadata (dict): an arbitrary dictionary to associate with all
                components of this type.

        Returns:
            A decorator function used to define components of the new type.
    """

    def decorator(*requires, **kwargs):
        optional = kwargs.get("optional", None)
        the_group = kwargs.get("group", group)
        component_type = kwargs.get("component_type", None)
        metadata = kwargs.get("metadata", {}) or {}

        requires = list(requires) or kwargs.get("requires", [])
        optional = optional or []

        requires.extend(auto_requires)
        optional.extend(auto_optional)

        component_metadata = {}
        component_metadata.update(type_metadata)
        component_metadata.update(metadata)

        def _f(func):
            delegate = delegate_class(func, requires, optional)
            delegate.group = the_group
            delegate.metadata = component_metadata
            if executor:
                delegate.executor = executor
            delegate.type = component_type or decorator
            register_component(delegate)
            return func
        return _f

    if name:
        decorator.__name__ = name
        s = inspect.stack()
        frame = s[1][0]
        mod = inspect.getmodule(frame) or sys.modules.get("__main__")
        if mod:
            decorator.__module__ = mod.__name__
            setattr(mod, name, decorator)

    return decorator


def run_order(components, broker):
    """ Returns components in an order that satisfies their dependency
        relationships.
    """
    return toposort_flatten(components)


def run(components=COMPONENTS[GROUPS.single], broker=None):
    """ Executes components in an order that satisfies their dependency
        relationships.
    """
    broker = broker or Broker()

    for component in run_order(components, broker):
        start = time.time()
        try:
            if component not in broker and component in DELEGATES:
                log.info("Trying %s" % get_name(component))
                result = DELEGATES[component](broker)
                broker[component] = result
        except MissingRequirements as mr:
            if log.isEnabledFor(logging.DEBUG):
                name = get_name(component)
                reqs = stringify_requirements(mr.requirements)
                log.debug("%s missing requirements %s" % (name, reqs))
            broker.add_exception(component, mr)
        except SkipComponent:
            if log.isEnabledFor(logging.DEBUG):
                log.debug("%s raised SkipComponent" % get_name(component))
        except Exception as ex:
            if log.isEnabledFor(logging.DEBUG):
                log.debug(ex)
            broker.add_exception(component, ex, traceback.format_exc())
        finally:
            broker.exec_times[component] = time.time() - start
            broker.fire_observers(component)

    return broker


def run_incremental(components=COMPONENTS[GROUPS.single], broker=None):
    """ Executes components in an order that satisfies their dependency
        relationships. Disjoint subgraphs are executed one at a time and
        a broker containing the results for each is yielded. If a broker
        is passed here, its instances are used to seed the broker used
        to hold state for each sub graph.
    """
    seed_broker = broker or Broker()
    for graph in get_subgraphs(components):
        broker = Broker(seed_broker)
        yield run(graph, broker)
