import numpy as np
import uuid

# import math
from dask import delayed
import itertools
from functools import partial

DEFAULT_DATATYPE = np.float32


class Node(object):
    """
    Attributes
    ----------
    values : numpy array or None
             stores generated values
    """
    def __init__(self, name, *parents):
        self.name = name
        self.parents = []
        self.children = set()
        for p in list(parents):
            self.add_parent(p)

    def add_parents(self, nodes):
        for n in self.node_list(nodes):
            self.add_parent(n)

    def add_parent(self, node, index=None):
        node = self.ensure_node(node)
        if index is None:
            index = len(self.parents)
        self.parents.insert(index, node)
        node.children.add(self)

    def add_children(self, nodes):
        for n in set(self.node_list(nodes)):
            self.add_child(n)

    def add_child(self, node):
        node = self.ensure_node(node)
        node.add_parent(self)

    def is_root(self):
        return len(self.parents) == 0

    def is_leaf(self):
        return len(self.children) == 0

    def remove(self, keep_parents=False, keep_children=False):
        if not keep_parents:
            for i in range(len(self.parents)):
                self.remove_parent(i)
        if not keep_children:
            for c in self.children:
                c.remove_parent(self)

    def remove_parent(self, parent_or_index=None):
        index = parent_or_index
        if isinstance(index, Node):
            for i, p in enumerate(self.parents):
                if p == parent_or_index:
                    index = i
                    break
        if isinstance(index, Node):
            raise Exception("Could not find a parent")
        parent = self.parents[index]
        del self.parents[index]
        parent.children.remove(self)
        return index

    def replace_by(self, node, transfer_parents=True, transfer_children=True):
        """

        Parameters
        ----------
        node : Node
        transfer_parents
        transfer_children

        Returns
        -------

        """
        if transfer_parents:
            parents = self.parents.copy()
            for p in parents:
                self.remove_parent(p)
            node.add_parents(parents)

        if transfer_children:
            children = self.children.copy()
            for c in children:
                index = c.remove_parent(self)
                c.add_parent(node, index=index)

    @property
    def component(self):
        """Depth first search"""
        c = {}
        search = [self]
        while len(search) > 0:
            current = search.pop()
            if current.name in c:
                continue
            c[current.name] = current
            search += list(current.neighbours)
        return list(c.values())

    #@property
    #def graph(self):
    #    return Graph(self)

    @property
    def label(self):
        return self.name

    @property
    def neighbours(self):
        n = set(self.children)
        n = n.union(self.parents)
        return list(n)

    """Private methods"""

    def convert_to_node(self, obj, name):
        raise ValueError("No conversion to Node for value {}".format(obj))

    def ensure_node(self, obj):
        if isinstance(obj, Node):
            return obj
        name = "_{}_{}".format(self.name, str(uuid.uuid4().hex[0:6]))
        return self.convert_to_node(obj, name)

    """Static methods"""

    @staticmethod
    def node_list(nodes):
        if isinstance(nodes, dict):
            nodes = nodes.values()
        elif isinstance(nodes, Node):
            nodes = [nodes]
        return nodes


def to_slice(item):
    if not isinstance(item, slice):
        item = slice(item, item + 1)
    return item


class OutputSlice:
    """
    Similar to the standard slice object but without step. Holds futures for upcoming
    values.
    """
    def __init__(self):
        self._outputs = {}

    def __len__(self):
        len = 0
        for key in self._outputs:
            len += key[2]
        return len

    def add(self, output):
        self._outputs[output.key] = output

    def __getitem__(self, sl):
        """
        Currently supports only exact match with the sub slices
        """
        sl = to_slice(sl)
        # Filter all the relevant outputs
        outputs = {k[1]: output for k, output in self._outputs.items() if sl.start <= k[1] < sl.stop}
        # Sort
        outputs = [output for k, output in sorted(outputs.items())]
        # Map step (just take the data out of the output)
        outputs = [output['data'] for output in outputs]
        # "Reduce" step, basically just stack the output slices together
        if len(outputs) > 1:
            return delayed(np.vstack)(tuple(outputs))
        elif len(outputs) == 1:
            return outputs[0]
        else:
            raise IndexError


def to_output(input, **kwargs):
    output = input.copy()
    for k, v in kwargs.items():
        output[k] = v
    return output


substreams = itertools.count()


class Operation(Node):
    def __init__(self, name, operation, *parents):
        super(Operation, self).__init__(name, *parents)
        self.operation = operation

        self._index = 0
        self._store = OutputSlice()
        # Fixme: maybe move this to model
        self.seed = 0

    def generate(self, n, starting=None, batch_size=None):
        """
        Shorthand for generating n new values from the node
        """
        starting = self._index if starting is None else starting
        ending = starting + n
        batch_size = batch_size or n

        # Fill store up to `ending` in batches
        while len(self._store) < ending:
            l = len(self._store)
            n_batch = min(ending-l, batch_size)
            new_sl = slice(l, l+n_batch)
            new_output = self._generate_new_output(new_sl)
            self._store.add(new_output)

        self._index = max(self._index, ending)
        return self[slice(starting, ending)]

    def __getitem__(self, sl):
        sl = to_slice(sl)
        if len(self._store) < sl.stop:
            new_sl = slice(len(self._store), sl.stop)
            new_output = self._generate_new_output(new_sl)
            self._store.add(new_output)

        return self._store[sl]

    def get_input_dict(self, sl):
        n = sl.stop - sl.start
        input_data = tuple([p[sl] for p in self.parents])
        return {
            'data': input_data,
            'n': n,
            'index': sl.start,
        }

    def _generate_new_output(self, sl):
        input_dict = self.get_input_dict(sl)
        input = delayed(input_dict, pure=True)
        return delayed(self.operation)(input,
                                       dask_key_name=(self.name, sl.start, input_dict['n']))

    def convert_to_node(self, obj, name):
        return Constant(name, obj)


class Constant(Operation):
    def __init__(self, name, value):
        value = np.array(value, ndmin=1)
        super(Constant, self).__init__(name, lambda input: {'data': value})


"""
Operation mixins add additional functionality to the Operation class.
They do not define the actual operation. They only add keyword arguments.
"""


def set_substream(seed, sub_index):
    # return np.random.RandomState(seed).get_state()
    # Fixme: set substreams properly
    return np.random.RandomState(seed+sub_index).get_state()


class RandomStateMixin(Operation):
    """
    Makes Operation node stochastic
    """
    def __init__(self, *args, **kwargs):
        super(RandomStateMixin, self).__init__(*args, **kwargs)
        # Fixme: define where the seed comes from
        self.seed = 0

    def get_input_dict(self, sl):
        dct = super(RandomStateMixin, self).get_input_dict(sl)
        dct['random_state'] = self._get_random_state()
        return dct

    def _get_random_state(self):
        i_subs = next(substreams)
        return delayed(set_substream, pure=True)(self.seed, i_subs)


class ObservedMixin(Operation):
    """
    Adds observed data to the class
    """

    def __init__(self, *args, observed=None, **kwargs):
        super(ObservedMixin, self).__init__(*args, **kwargs)
        if observed is None:
            observed = self._inherit_observed()
        self.observed = np.array(observed, ndmin=2)

    def _inherit_observed(self):
        if len(self.parents) and hasattr(self.parents[0], 'observed'):
            observed = tuple([p.observed for p in self.parents])
            observed = self.operation({'data': observed})['data']
        else:
            raise ValueError('There is no observed value to inherit')
        return observed


"""
ABC specific Operation nodes
"""


# For python simulators using numpy random variables
def simulator_operation(simulator, input):
    # set the random state
    prng = np.random.RandomState(0)
    prng.set_state(input['random_state'])
    data = simulator(*input['data'], prng=prng)
    return to_output(input, data=data, random_state=prng.get_state())


# TODO: make a decorator for these classes that wrap the operation wrappers (such as the simulator_operation)
class Simulator(ObservedMixin, RandomStateMixin, Operation):
    def __init__(self, name, simulator, *args, **kwargs):
        operation = partial(simulator_operation, simulator)
        super(Simulator, self).__init__(name, operation, *args, **kwargs)


def summary_operation(operation, input):
    data = operation(*input['data'])
    return to_output(input, data=data)


class Summary(ObservedMixin, Operation):
    def __init__(self, name, operation, *args, **kwargs):
        operation = partial(summary_operation, operation)
        super(Summary, self).__init__(name, operation, *args, **kwargs)


def discrepancy_operation(operation, input):
    data = operation(input['data'], input['observed'])
    return to_output(input, data=data)


class Discrepancy(Operation):
    """
    The operation input has a tuple of data and tuple of observed
    """
    def __init__(self, name, operation, *args):
        operation = partial(discrepancy_operation, operation)
        super(Discrepancy, self).__init__(name, operation, *args)

    def get_input_dict(self, sl):
        dct = super(Discrepancy, self).get_input_dict(sl)
        dct['observed'] = observed = tuple([p.observed for p in self.parents])
        return dct


def threshold_operation(threshold, input):
    data = input['data'][0] < threshold
    return to_output(input, data=data)


class Threshold(Operation):
    def __init__(self, name, threshold, *args):
        operation = partial(threshold_operation, threshold)
        super(Threshold, self).__init__(name, operation, *args)


"""
Other functions
"""


def fixed_expand(n, fixed_value):
    """
    Creates a new axis 0 (or dimension) along which the value is repeated
    """
    return np.repeat(fixed_value[np.newaxis,:], n, axis=0)







# class Graph(object):
#     """A container for the graphical model"""
#     def __init__(self, anchor_node=None):
#         self.anchor_node = anchor_node
#
#     @property
#     def nodes(self):
#         return self.anchor_node.component
#
#     def sample(self, n, parameters=None, threshold=None, observe=None):
#         raise NotImplementedError
#
#     def posterior(self, N):
#         raise NotImplementedError
#
#     def reset(self):
#         data_nodes = self.find_nodes(Data)
#         for n in data_nodes:
#             n.reset()
#
#     def find_nodes(self, node_class=Node):
#         nodes = []
#         for n in self.nodes:
#             if isinstance(n, node_class):
#                 nodes.append(n)
#         return nodes
#
#     def __getitem__(self, key):
#         for n in self.nodes:
#             if n.name == key:
#                 return n
#         raise IndexError
#
#     def __getattr__(self, item):
#         for n in self.nodes:
#             if n.name == item:
#                 return n
#         raise AttributeError
#
#     def plot(self, graph_name=None, filename=None, label=None):
#         from graphviz import Digraph
#         G = Digraph(graph_name, filename=filename)
#
#         observed = {'shape': 'box', 'fillcolor': 'grey', 'style': 'filled'}
#
#         # add nodes
#         for n in self.nodes:
#             if isinstance(n, Fixed):
#                 G.node(n.name, xlabel=n.label, shape='point')
#             elif hasattr(n, "observed") and n.observed is not None:
#                 G.node(n.name, label=n.label, **observed)
#             # elif isinstance(n, Discrepancy) or isinstance(n, Threshold):
#             #     G.node(n.name, label=n.label, **observed)
#             else:
#                 G.node(n.name, label=n.label, shape='doublecircle',
#                        fillcolor='deepskyblue3',
#                        style='filled')
#
#         # add edges
#         edges = []
#         for n in self.nodes:
#             for c in n.children:
#                 if (n.name, c.name) not in edges:
#                     edges.append((n.name, c.name))
#                     G.edge(n.name, c.name)
#             for p in n.parents:
#                 if (p.name, n.name) not in edges:
#                     edges.append((p.name, n.name))
#                     G.edge(p.name, n.name)
#
#         if label is not None:
#             G.body.append("label=" + '\"' + label + '\"')
#
#         return G
#
#     """Properties"""
#
#     @property
#     def thresholds(self):
#         return self.find_nodes(node_class=Threshold)
#
#     @property
#     def discrepancies(self):
#         return self.find_nodes(node_class=Discrepancy)
#
#     @property
#     def simulators(self):
#         return [node for node in self.nodes if isinstance(node, Simulator)]
#
#     @property
#     def priors(self):
#         raise NotImplementedError
#         #Implementation wrong, prior have Value nodes as hyperparameters
#         # priors = self.find_nodes(node_class=Stochastic)
#         # priors = {n for n in priors if n.is_root()}
#         # return priors
