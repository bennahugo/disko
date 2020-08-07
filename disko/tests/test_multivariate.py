#
# Copyright Tim Molteno 2019 tim@elec.ac.nz
#

import unittest
import logging
import os

import numpy as np

from disko import multivariate_gaussian as mg

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler()) # Add a null handler so logs can go somewhere
LOGGER.setLevel(logging.INFO)

class TestMultivariate(unittest.TestCase):


    def test_linear(self):
        # Test a really silly example of a linear transformation
        
        mu = np.zeros((1))
        sigma = np.zeros((1,1))
        sigma[0,0] = 1
        x = mg.MultivariateGaussian(mu+1, sigma)

        y = x.linear_transform(sigma*3, mu+2)
        
        self.assertAlmostEqual(y.sigma[0,0], 3)
        self.assertAlmostEqual(y.mu[0], 5)

    def test_sampling(self):
        # Test a really silly example of a linear transformation
        
        mu = np.zeros((1))
        sigma = np.zeros((1,1))
        sigma[0,0] = 2
        x = mg.MultivariateGaussian(mu+1, sigma)

        samples = []
        N = 25000
        for i in range(N):
            samples.append(x.sample())
        
        samples = np.array(samples)
        LOGGER.info(np.mean(samples))
        LOGGER.info(np.std(samples))
    
        precision = 1
        self.assertAlmostEqual(np.mean(samples), x.mu[0], precision)
        self.assertAlmostEqual(np.std(samples)**2, x.sigma[0,0], precision)
        
    
