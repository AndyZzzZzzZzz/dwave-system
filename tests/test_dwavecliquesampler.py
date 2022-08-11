# Copyright 2020 D-Wave Systems Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import itertools
import unittest
import unittest.mock

import dimod
import dwave_networkx as dnx

from dwave.cloud.exceptions import SolverOfflineError, SolverNotFoundError

from dwave.system import DWaveCliqueSampler, DWaveSampler


class MockDWaveSampler(dimod.RandomSampler, dimod.Structured):
    # contains the minimum needed to work with DWaveCliqueSampler

    edgelist = None
    nodelist = None

    def __init__(self, **kwargs):
        self.properties = dict(h_range=[-2, 2],
                               j_range=[-1, 1],
                               extended_j_range=[-2, 1],)
        self.parameters = {'auto_scale': None}

    def sample(self, bqm, auto_scale=True):
        assert not auto_scale
        assert bqm.vartype is dimod.SPIN

        h_range = self.properties['h_range']
        j_range = self.properties['extended_j_range']

        for bias in bqm.linear.values():
            assert h_range[0] <= bias <= h_range[1]

        for bias in bqm.quadratic.values():
            assert j_range[0] <= bias <= j_range[1]

        return super().sample(bqm)

    def trigger_failover(self):
        pass

    to_networkx_graph = DWaveSampler.to_networkx_graph


class MockChimeraDWaveSampler(MockDWaveSampler):
    def __init__(self, **config):
        super().__init__()

        self.properties.update(topology=dict(shape=[4, 4, 4], type='chimera'))

        G = dnx.chimera_graph(4, 4, 4)

        self.nodelist = list(G.nodes)
        self.edgelist = list(G.edges)

    def sample(self, bqm, **kwargs):

        # per_qubit_coupling_range
        ran = (-9, 6)

        # check the total coupling range
        for v in bqm.variables:
            bias = sum(bqm.adj[v].values())
            assert ran[0] <= bias <= ran[1]

        return super().sample(bqm, **kwargs)


class MockPegasusDWaveSampler(MockDWaveSampler):
    def __init__(self, **config):
        super().__init__()

        self.properties.update(topology=dict(shape=[6], type='pegasus'))

        G = dnx.pegasus_graph(6)

        self.nodelist = list(G.nodes)
        self.edgelist = list(G.edges)


with unittest.mock.patch('dwave.system.samplers.clique.DWaveSampler',
                         MockChimeraDWaveSampler):
    chimera_sampler = DWaveCliqueSampler()

with unittest.mock.patch('dwave.system.samplers.clique.DWaveSampler',
                         MockPegasusDWaveSampler):
    pegasus_sampler = DWaveCliqueSampler()


@dimod.testing.load_sampler_bqm_tests(chimera_sampler)
@dimod.testing.load_sampler_bqm_tests(pegasus_sampler)
class TestDWaveCliqueSampler(unittest.TestCase):
    def test_api(self):
        dimod.testing.assert_sampler_api(chimera_sampler)
        dimod.testing.assert_sampler_api(pegasus_sampler)

    def test_clique(self):
        self.assertEqual(len(chimera_sampler.clique(2)), 2)
        self.assertEqual(len(chimera_sampler.clique(16)), 16)

        self.assertEqual(len(chimera_sampler.clique(['a', 1])), 2)
        self.assertEqual(set(chimera_sampler.clique(['a', 1])), {'a', 1})

    def test_largest_clique(self):
        self.assertEqual(len(chimera_sampler.largest_clique()), 16)

    def test_ferromagnet_chimera(self):
        # submit a maximum ferromagnet
        bqm = dimod.BinaryQuadraticModel('SPIN')
        for u, v in itertools.combinations(chimera_sampler.largest_clique(), 2):
            bqm.quadratic[u, v] = -1

        chimera_sampler.sample(bqm).resolve()

    def test_too_large(self):
        num_variables = chimera_sampler.largest_clique_size + 1

        bqm = dimod.BinaryQuadraticModel(num_variables, 'SPIN')

        with self.assertRaises(ValueError):
            chimera_sampler.sample(bqm)

    def test_qubit_coupling_range(self):
        n = pegasus_sampler.largest_clique_size

        bqm = dimod.BinaryQuadraticModel({}, 
                {(u, v): -2 for u in range(n) for v in range(u+1, n)}, 'SPIN')

        pegasus_sampler.sample(bqm, chain_strength=-0.5).resolve()



class TestFailover(unittest.TestCase):
    @unittest.mock.patch('dwave.system.samplers.clique.DWaveSampler',
                         MockChimeraDWaveSampler)
    def test_default(self):
        sampler = DWaveCliqueSampler()

        def mocksample(*args, **kwargs):
            raise SolverOfflineError

        sampler.child.sample = mocksample

        with self.assertRaises(SolverOfflineError):
            sampler.sample_ising({}, {})

    @unittest.mock.patch('dwave.system.samplers.clique.DWaveSampler',
                         MockChimeraDWaveSampler)
    def test_noretry(self):
        sampler = DWaveCliqueSampler(failover=True, retry_interval=-1)

        def mocksample(*args, **kwargs):
            raise SolverOfflineError

        sampler.child.sample = mocksample

        def mocktrigger(*args, **kwargs):
            raise SolverNotFoundError

        sampler.child.trigger_failover = mocktrigger

        with self.assertRaises(SolverNotFoundError):
            sampler.sample_ising({}, {})

    @unittest.mock.patch('dwave.system.samplers.clique.DWaveSampler',
                         MockChimeraDWaveSampler)
    def test_properties(self):
        sampler = DWaveCliqueSampler(failover=True)

        def mocksample(*args, **kwargs):
            count = getattr(mocksample, 'count', 0)

            if count:
                return dimod.SampleSet.from_samples([], energy=0., vartype='SPIN')
            else:
                mocksample.count = count + 1
                raise SolverOfflineError

        sampler.child.sample = mocksample

        G = sampler.target_graph
        qlr = sampler.qpu_linear_range
        qqr = sampler.qpu_quadratic_range

        self.assertIs(G, sampler.target_graph)
        self.assertIs(qlr, sampler.qpu_linear_range)
        self.assertIs(qqr, sampler.qpu_quadratic_range)

        sampler.sample_ising({}, {})

        self.assertIsNot(G, sampler.target_graph)
        self.assertIsNot(qlr, sampler.qpu_linear_range)
        self.assertIsNot(qqr, sampler.qpu_quadratic_range)
