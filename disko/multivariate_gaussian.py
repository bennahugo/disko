import numpy as np

class MultivariateGaussian:
    '''
        Handle a multivariate gaussian with D dimensions, and real entries.
        
        The basic idea is to take :
            * measurements v_0, with covariance sigma_v
            * model A, such that v = A x
        
        lh = MultivariateGaussian(v_0, sigma_v) % model for the measurement process, PDF for a measurmeent v
        
        prior = MultivariateGaussian(mu_x, sigma_x)   % distribution over vectors x
        
        posterior = bayes_update(lh, prior)
        
    '''
    def __init__(self, mu, sigma):
        '''
            Create a D-dimensional multivariate Gaussian with known mean and standard deviation
        '''
        try:
            self.D = mu.shape[0]
        except:
            raise ValueError('Mean mu {} must be a vector'.format(mu.shape))
        self.mu = mu
        self.sigma = sigma
        self.dtype = np.float64
        
        if (sigma.shape[0] != self.D) or (sigma.shape[1] != self.D):
            raise ValueError('Covariance sigma {} must be a {}x{} square matrix'.format(sigma.shape, self.D))
    
        self._sigma_inv = np.linalg.inv(self.sigma)
    
    def sigma_inv(self):
        if self._sigma_inv is None:
            self._sigma_inv = np.linalg.inv(self.sigma)
        return self._sigma_inv

    def bayes_update(self, lh, prior, measurements):
        '''
            Return a new MultivariateGaussian, after update by likelihood, and measurements
            
            @param lh, prior
            
            See section 3.1 of the documentation
        '''
        sigma_1 = np.linalg.inv(lh.sigma_inv() + prior.sigma_inv())
        mu_1 = sigma_1 @ (prior.sigma_inv() @ self.mu + lh.sigma_inv() @ measurements)
    
    
    def linear_transform(self, A, b):
        '''
            Linear transform
            y = A x + b
        '''
        sigma_1 = A @ self.sigma @ self.sigma.T
        mu_1 = A @ self.mu + b
        return MultivariateGaussian(mu_1, sigma_1)
        
