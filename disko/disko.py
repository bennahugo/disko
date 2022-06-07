#!/usr/bin/env python
#
# The DiSkO algorithm for imaging without gridding.
#
# Tim Molteno 2017-2019 tim@elec.ac.nz
#
import os
import argparse
import sys
import threading
import datetime
import json
import logging
import time
import pylops
import scipy

import numpy as np
import healpy as hp
#import dask.array as da


from copy import deepcopy
from scipy.optimize import minimize
from sklearn import linear_model
from sklearn.metrics import mean_squared_error

from tart.imaging import elaz
from tart.util import constants


from .sphere import HealpixSphere
from .ms_helper import read_ms
from .multivariate_gaussian import MultivariateGaussian

logger = logging.getLogger(__name__)
logger.addHandler(
    logging.NullHandler()
)  # Add other handlers if you're using this as a library
logger.setLevel(logging.INFO)

"""
    Little helper function to get the UVW positions from the antennas positions.
    The test (i != j) can be changed to (i > j) to avoid the duplicated conjugate
    measurements.
"""


def get_all_uvw(ant_pos):
    """
    ant pos is an array of (N_ant, 3)
    """
    # logger.info(f"get_all_uvw({ant_pos})")
    if ant_pos.shape[1] != 3:
        raise RuntimeError(
            "Ant pos (shape={}) must be an array of (N_ant, 3)".format(ant_pos.shape)
        )
    baselines = []
    num_ant = len(ant_pos)
    ant_p = np.array(ant_pos)
    for i in range(num_ant):
        for j in range(num_ant):
            if i < j:
                baselines.append([i, j])

    bl_pos = ant_p[np.array(baselines).astype(int)]
    uu_a, vv_a, ww_a = (bl_pos[:, 0] - bl_pos[:, 1]).T
    return baselines, uu_a, vv_a, ww_a


def to_column(x):
    return x.reshape([-1, 1])


def vis_to_real(vis_arr):
    return np.concatenate((np.real(vis_arr), np.imag(vis_arr)))


def get_source_list(source_json, el_limit, jy_limit):
    src_list = []
    if source_json is not None:
        src_list = elaz.from_json(source_json, el_limit=el_limit, jy_limit=jy_limit)
    return src_list


REAL_DATATYPE = np.float64
COMPLEX_DATATYPE = np.complex128

C = 2.99793e8


def omega(freq):
    r"""
    Little routine to convert a frequency into omega
    """
    wavelength = C / freq
    return 2 * np.pi / wavelength


def jomega(freq):
    r"""
    Little routine to convert a frequency into j*omega
    """
    return 1.0j * omega(freq)


def get_harmonic(p2j, in_sphere, u, v, w):
    harmonic = (
        np.exp(p2j * (u * in_sphere.l + v * in_sphere.m + w * in_sphere.n_minus_1))
        * in_sphere.pixel_areas
    )
    return harmonic

def fastmatvec(x,freq,u_arr, v_arr, w_arr, l, m, n_minus_1, pixel_areas):
    """
    Multiply by the sky x, producing the set of measurements y
    Returns returns A * x.

    ( v_real    = (T_real   x
        v_imag )     T_imag)
    """
    
    n_u = u_arr.shape[0]
    
    y_re = []
    y_im = []
    
    for f in freq:
        p2 = omega(f)

        for i in range(n_u):
            u = u_arr[i]
            v = v_arr[i]
            w = w_arr[i]
            
            z = -p2 * (u*l + v*m + w*n_minus_1)
            re = np.cos(z)*pixel_areas
            im = np.sin(z)*pixel_areas
            y_re.append(np.dot(x, re))
            y_im.append(np.dot(x, im))

    return np.concatenate((np.array(y_re), np.array(y_im)))

import scipy.sparse.linalg as spalg

class DiSkOOperator(pylops.LinearOperator):
    """
    Linear operator for the telescope with a discrete sky
    """

    def __init__(self, u_arr, v_arr, w_arr, data, frequencies, sphere):
        self.N = sphere.npix  # Number of pixels
        self.u_arr = u_arr
        self.v_arr = v_arr
        self.w_arr = w_arr
        self.dtype = REAL_DATATYPE

        try:
            self.n_v, self.n_freq, self.npol = data.shape
        except:
            raise RuntimeError("Data must be of the shape [n_v*2, n_freq, n_pol]")

        if self.n_v != len(self.u_arr) * 2:
            raise RuntimeError(
                "Vis data must be split into [real, imag] {} {}".format(
                    self.n_v, self.u_arr.shape
                )
            )

        self.M = self.n_v * self.n_freq

        self.frequencies = np.array(frequencies)
        self.sphere = sphere

        self.shape = (self.M, self.N)
        self.explicit = False  # Can't be directly inverted
        logger.info("Creating DiSkOOperator data={}".format(self.shape))

    def A(self, i, j, p2j):
        n_vis = len(self.u_arr)
        u, v, w = (
            self.u_arr[i % n_vis],
            self.v_arr[i % n_vis],
            self.w_arr[i % n_vis],
        )  # the row index (one u,v,w element per vis)
        l, m, n = (
            self.sphere.l[j],
            self.sphere.m[j],
            self.sphere.n[j],
        )  # The column index (one l,m,n element per pixel)

        z = np.exp(-p2j * (u * l + v * m + w * (n - 1))) * self.sphere.pixel_areas
        if i < n_vis:
            return np.real(z)
        else:
            return np.imag(z)

    def Ah(self, i, j, p2j):
        return np.conj(self.A(j, i, p2j))

    def _matvec(self, x):
        
        return fastmatvec(x, self.frequencies, 
                          self.u_arr, self.v_arr, self.w_arr, 
                          self.sphere.l, self.sphere.m, self.sphere.n_minus_1, 
                          self.sphere.pixel_areas)
        """
        Multiply by the sky x, producing the set of measurements y
        Returns returns A * x.

        ( v_real    = (T_real   x
          v_imag )     T_imag)
        """
        if True:
            y = []
            for f in self.frequencies:
                p2j = jomega(f)

                #for u, v, w in zip(
                    #self.u_arr, self.v_arr, self.w_arr
                #):  # For all complex vis
                for i in range(self.u_arr.shape[0]):
                    u = self.u_arr[i]
                    v = self.v_arr[i]
                    w = self.w_arr[i]
                    column = (
                        np.exp(
                            -p2j
                            * (
                                u * self.sphere.l
                                + v * self.sphere.m
                                + w * self.sphere.n_minus_1
                            )
                        )
                        * self.sphere.pixel_areas
                    )
                    y.append(np.dot(x, column))

            y = np.array(y)
            return vis_to_real(y)
        else:
            y_re = []
            y_im = []
            for f in self.frequencies:
                p2 = omega(f)

                for u, v, w in zip(
                    self.u_arr, self.v_arr, self.w_arr
                ):  # For all complex vis
                    theta = -p2 * (
                        u * self.sphere.l
                        + v * self.sphere.m
                        + w * self.sphere.n_minus_1
                    )  # harmonics
                    re = np.cos(theta) * self.sphere.pixel_areas
                    im = np.sin(theta) * self.sphere.pixel_areas
                    y_re.append(np.dot(x, re))
                    y_im.append(np.dot(x, im))

            y = np.concatenate((np.array(y_re), np.array(y_im)))
            return y

    def _rmatvec(self, v):
        r"""
        Returns x = A^H * v, where A^H is the conjugate transpose of A.

        x = ( T_real' T_imag') (v_real
                                v_imag)
        """
        assert v.shape == (self.M,)
        n_vis = self.M // 2

        vis_complex = v[0:n_vis] + 1.0j * v[n_vis:]
        ret = []

        for f in self.frequencies:
            p2j = jomega(f)
            p2 = omega(f)
            # Vector version
            for l, m, n_1 in zip(
                self.sphere.l, self.sphere.m, self.sphere.n_minus_1
            ):  # for each pixel
                if False:
                    column = (
                        np.exp(
                            p2j * (self.u_arr * l + self.v_arr * m + self.w_arr * n_1)
                        )
                        * self.sphere.pixel_areas
                    )
                    ret.append(np.dot(vis_complex, column))
                else:
                    theta = -p2 * (self.u_arr * l + self.v_arr * m + self.w_arr * n_1)

                    re = np.cos(theta) * self.sphere.pixel_areas
                    im = np.sin(theta) * self.sphere.pixel_areas

                    reim = np.concatenate((re, im))
                    assert reim.shape == (self.M,)
                    ret.append(np.dot(v, reim))

        return np.array(ret)


class DirectImagingOperator(pylops.LinearOperator):
    r"""
    This is the approximate inverse of the DiSkOOperator, and corresponds to
    imaging by the discrete fourier transform
    """

    def __init__(self, u_arr, v_arr, w_arr, data, frequencies, sphere):
        self.N = sphere.npix  # Number of pixels
        self.u_arr = u_arr
        self.v_arr = v_arr
        self.w_arr = w_arr
        self.dtype = REAL_DATATYPE

        try:
            self.n_v, self.n_freq, self.npol = data.shape
        except:
            raise RuntimeError("Data must be of the shape [n_v, n_freq, n_pol]")

        self.M = self.n_v * self.n_freq

        self.frequencies = frequencies
        self.sphere = sphere

        self.shape = (self.N, self.M)
        self.explicit = False  # Can't be directly inverted
        logger.info("Creating DirectImagingOperator data={}".format(self.shape))

    def _matvec(self, v):
        """
        Multiply by the measurements v, producing the sky
        Returns returns A * v.

        This is treating the v as a vector in a vector space where the basis vectors are the harmonics. This operator is a transformation from v to R^N

        sky = sum_v h_i * v_i

        What do the rows and columns of this matrix look like?

        The ajoint is just the conjugated basis vectors as rows.
        """
        sky = np.zeros(self.N, dtype=self.dtype)

        for f in self.frequencies:
            p2j = jomega(f)

            for u, v, w, vis in zip(self.u_arr, self.v_arr, self.w_arr, v):
                h = get_harmonic(p2j, self.sphere, u, v, w)
                sky += vis * h

        return sky

    def _rmatvec(self, x):
        r"""
        To get this we must recover the visibilities from a sky vector x. These
        are simply the inner products with the harmonics (conjugated) with x

        v = \sum_uvw h_i \dot x
        """
        assert x.shape == (self.N,)

        ret = []
        for f in self.frequencies:
            p2j = jomega(f)

            # Vector version
            for u, v, w in zip(self.u_arr, self.v_arr, self.w_arr):
                h = get_harmonic(-p2j, self.sphere, u, v, w)
                ret.append(np.dot(x, h))

        return np.array(ret)


class DiSkO(object):
    def __init__(self, u_arr, v_arr, w_arr, frequency):
        self.harmonics = {}  # Temporary store for harmonics
        self.u_arr = u_arr
        self.v_arr = v_arr
        self.w_arr = w_arr
        self.frequency = frequency
        self.n_v = len(self.u_arr)

    @classmethod
    def from_ant_pos(cls, ant_pos, frequency):
        ## Get u, v, w from the antenna positions
        baselines, u_arr, v_arr, w_arr = get_all_uvw(ant_pos)
        ret = cls(u_arr, v_arr, w_arr, frequency)
        ret.info = {}
        return ret

    @classmethod
    def from_ms(cls, ms, num_vis, res_arcmin, chunks=50000, channel=0, field_id=0):
        u_arr, v_arr, w_arr, frequency, cv_vis, hdr, tstamp, rms, indices = read_ms(
            ms, num_vis, res_arcmin, chunks, channel, field_id
        )

        ret = cls(u_arr, v_arr, w_arr, frequency)
        ret.vis_arr = cv_vis  # np.array(cv_vis, dtype=COMPLEX_DATATYPE)
        ret.timestamp = tstamp
        ret.rms = rms
        ret.info = hdr
        ret.indices = indices

        return ret

    def vis_stats(self):
        vabs = np.abs(self.vis_arr)

        p05, p50, p95, p100 = np.percentile(vabs, [5, 50, 95, 100])
        logger.info(
            "Vis Range: [{:5.4g} {:5.4g} {:5.4g} {:5.4g}]".format(p05, p50, p95, p100)
        )

        logger.info("Vis Energy: {:5.4g}".format(np.sum(vabs)))

        return p05, p50, p95, p100

    @classmethod
    def from_cal_vis(cls, cal_vis):

        c = cal_vis.get_config()
        ant_p = np.asarray(c.get_antenna_positions())

        # We need to get the vis array to be correct for the full set of u,v,w points (baselines),
        # including the -u,-v, -w points.

        baselines, u_arr, v_arr, w_arr = get_all_uvw(ant_p)

        ret = cls(u_arr, v_arr, w_arr, c.get_operating_frequency())
        ret.vis_arr = []
        for bl in baselines:
            v = cal_vis.get_visibility(bl[0], bl[1])  # Handles the conjugate bit
            ret.vis_arr.append(v)
            # logger.info("vis={}, bl={}".format(v, bl))
        ret.vis_arr = np.array(ret.vis_arr, dtype=COMPLEX_DATATYPE)
        ret.info = {}
        return ret

    def get_harmonics(self, in_sphere):
        """Create the harmonics for this arrangement of sphere pixels"""
        # cache_key = "{}:".format(in_sphere.npix)
        # if (cache_key in self.harmonics):
        # return self.harmonics[cache_key]

        harmonic_list = []
        p2j = jomega(self.frequency)

        # logger.info("pixel areas:  {}".format(in_sphere.pixel_areas))
        for u, v, w in zip(self.u_arr, self.v_arr, self.w_arr):
            harmonic = (
                np.exp(
                    p2j * (u * in_sphere.l + v * in_sphere.m + w * in_sphere.n_minus_1)
                )
                * in_sphere.pixel_areas
            )
            assert harmonic.shape[0] == in_sphere.npix
            harmonic_list.append(harmonic)
        # self.harmonics[cache_key] = harmonic_list

        # assert(harmonic_list[0].shape[0] == in_sphere.npix)
        return harmonic_list

    def image_visibilities(self, vis_arr, sphere):
        """
        Create a DiSkO image from visibilities using the direct ajoint of the
        measurement operator (corresponds to the inverse DFT)

        Args:

            vis_arr (np.array): An array of complex visibilities
            sphere (int):       he healpix sphere.
        """

        assert len(vis_arr) == len(self.u_arr)
        logger.info("Imaging Visabilities nside={}".format(sphere.nside))
        t0 = time.time()

        pixels = np.zeros(sphere.npix, dtype=COMPLEX_DATATYPE)
        harmonic_list = self.get_harmonics(sphere)
        for h, vis in zip(harmonic_list, vis_arr):
            pixels += vis * h

        t1 = time.time()
        logger.info("Elapsed {}s".format(time.time() - t0))

        sphere.set_visible_pixels(np.abs(pixels))

        return pixels.reshape(-1, 1)

    def solve_vis(self, vis_arr, sphere, scale=True):

        logger.info("Solving Visabilities nside={}".format(sphere.nside))
        t0 = time.time()

        gamma = self.make_gamma(sphere)

        sky, residuals, rank, s = np.linalg.lstsq(
            gamma, to_column(vis_to_real(vis_arr))
        )

        logger.info("Elapsed {}s".format(time.time() - t0))

        sphere.set_visible_pixels(sky, scale)

        return sky.reshape(-1, 1)

    def vis_to_data(self, vis_arr=None):
        data = np.zeros((self.n_v * 2, 1, 1), dtype=REAL_DATATYPE)
        if vis_arr is not None:
            data[:, 0, 0] = vis_to_real(vis_arr)
        else:
            data[:, 0, 0] = vis_to_real(self.vis_arr)
            assert data.shape[0] == self.n_v * 2

        return data

    def solve_matrix_free(
        self, data, sphere, alpha=0.0, scale=True, fista=False, lsqr=True, lsmr=False, niter=25
    ):
        """
        data = [vis_arr, n_freq, n_pol]
        """
        logger.info(f"Solving Visabilities sphere={sphere} data={data.shape}")
        assert data.shape[0] == self.n_v * 2

        t0 = time.time()

        frequencies = [self.frequency]
        logger.info("frequencies: {}".format(frequencies))

        A = DiSkOOperator(self.u_arr, self.v_arr, self.w_arr, data, frequencies, sphere)
        Apre = DirectImagingOperator(
            self.u_arr, self.v_arr, self.w_arr, data, frequencies, sphere
        )
        d = data.flatten()

        logger.info("Data.shape {}".format(data.shape))

        # u,s,vt = spalg.svds(A, k=min(A.shape)-2)
        # logger.info("t ={}, s={}".format(time.time() - t0, s))
        if fista:
            if alpha < 0:
                alpha = 10**(-np.log10(self.n_v) + 2) ## Empirical fit

            sky, niter = pylops.optimization.sparsity.FISTA(
                A, d, tol=1e-2, niter=niter, alpha=alpha, show=True
            )

        if lsqr:
            if alpha < 0:
                alpha = np.mean(self.rms)
            (
                sky,
                lstop,
                itn,
                r1norm,
                r2norm,
                anorm,
                acond,
                arnorm,
                xnorm,
                var,
            ) = spalg.lsqr(A, data, damp=alpha)

            residual = d - A @ sky

            residual_norm, solution_norm = np.compute(
                np.linalg.norm(residual) ** 2, np.linalg.norm(sky) ** 2
            )

            # mse = mean_squared_error(reg.coef_, np.zeros_like(reg.coef_))
            # mser = mean_squared_error(vis_aux, gamma @ sky)

            logger.info(
                "Alpha: {}: Loss: {}: rnorm: {}: snorm: {}: mse: {}: mser: {}".format(
                    alpha, itn, r2norm, solution_norm, solution_norm, r2norm
                )
            )

            # logger.info("Matrix free solve elapsed={} x={}, stop={}, itn={} r1norm={}".format(time.time() - t0, sky.shape, lstop, itn, r1norm))
        if lsmr:
            if alpha < 0:
                alpha = np.mean(self.rms)
            x0 = Apre * d

            sky, info = pylops.optimization.leastsquares.NormalEquationsInversion(
                A, Regs=None, data=d, x0=x0, epsI=alpha, returninfo=True
            )
            # logger.info("Matrix free solve elapsed={} x={}, stop={}, itn={} r1norm={}".format(time.time() - t0, sky.shape, lstop, itn, r1norm))
            # logger.info("A M={} N={}".format(A.M, A.N))

            # sky, lstop, itn, normr, mormar, morma, conda, normx = spalg.lsmr(A, data, damp=alpha)
            # logger.info("Matrix free solve elapsed={} x={}, stop={}, itn={} normr={}".format(time.time() - t0, sky.shape, lstop, itn, normr))
        # sky = np.abs(sky)
        
        residual = d - A @ sky
        normalized_residuals = residual / np.std(residual)
        
        RESIDUAL_LIMIT = 10.0  # Arbitrary limit to show bad residuals.
        
        bigguns = np.where(normalized_residuals > RESIDUAL_LIMIT)
        
        logger.info(f"Residuals {normalized_residuals[bigguns]}")
        
        # Now reshape data back into complex data (from real appended to complex)
        c_data = np.reshape(data, (2, self.n_v))
        c_data = c_data[0] + 1.0J * c_data[1]
        
        c_res = np.reshape(normalized_residuals, (2, self.n_v))
        c_res = c_res[0] + 1.0J * c_res[1]

        bigguns = np.where(np.abs(c_res) > RESIDUAL_LIMIT)[0]
        logger.info(f"Residual problems {bigguns}")
        logger.info(f"Residual indices {self.indices[bigguns]}")
        for b in bigguns.tolist():
            logger.info(f"    {b}: {np.abs(c_res[b]):4.2f}: \t{self.u_arr[b]}, {self.v_arr[b]}, {self.w_arr[b]}: {c_data[b]}")
            
        sphere.set_visible_pixels(sky, scale)
        return sky.reshape(-1, 1)

    def make_gamma(self, sphere, makecomplex=False):

        logger.info("Making Gamma Matrix npix={}".format(sphere.npix))

        harmonic_list = self.get_harmonics(sphere)

        n_s = len(harmonic_list[0])
        n_v = len(harmonic_list)

        gamma = np.asarray(harmonic_list)  # , dtype=COMPLEX_DATATYPE)
        gamma = gamma.reshape((n_v, n_s))
        gamma = gamma.conj()  # .rechunk('auto')

        if makecomplex:
            return gamma

        # g_real = np.real(gamma).astype(REAL_DATATYPE)
        # g_imag = np.imag(gamma).astype(REAL_DATATYPE)
        g_real = np.real(gamma)
        g_imag = np.imag(gamma)
        ret = np.block([[g_real], [g_imag]])  # .rechunk('auto')

        logger.info("Gamma Shape: {}".format(gamma.shape))
        # for i, h in enumerate(harmonic_list):
        # gamma[i,:] = h[0]

        return ret

    def image_lasso(self, vis_arr, sphere, alpha, l1_ratio, scale=False, use_cv=False):
        gamma = self.make_gamma(sphere)

        vis_aux = vis_to_real(vis_arr)

        # Save proj operator for Further Analysis.
        if False:
            fname = "l1_big_files.npz"
            np.savez_compressed(
                fname, gamma_re=gamma, vis_re=np.real(vis_arr), vis_im=np.imag(vis_arr)
            )
            logger.info("Operator file {} saved".format(fname))

            logger.info("gamma = {}".format(gamma.shape))
            logger.info("vis_aux = {}".format(vis_aux.shape))

        n_s = sphere.pixels.shape[0]

        if not use_cv:
            reg = linear_model.ElasticNet(
                alpha=alpha / np.sqrt(n_s),
                l1_ratio=l1_ratio,
                tol=1e-6,
                max_iter=100000,
                positive=True,
            )
            reg.fit(gamma, vis_aux)

        else:
            reg = linear_model.ElasticNetCV(
                l1_ratio=l1_ratio, cv=5, max_iter=10000, positive=True
            )
            reg.fit(gamma, vis_aux)
            logger.info(
                "Cross Validation alpha: {} l1_ratio: {}".format(
                    reg.alpha_, reg.l1_ratio
                )
            )

        sky = reg.coef_
        logger.info("sky = {}".format(sky.shape))

        residual = vis_aux - gamma @ sky

        residual_norm = np.linalg.norm(residual) ** 2
        solution_norm = np.linalg.norm(sky) ** 2
        score = reg.score(gamma, vis_aux)

        logger.info(
            "Alpha: {}: Loss: {}: rnorm: {}: snorm: {}".format(
                alpha, score, residual_norm, solution_norm
            )
        )

        sphere.set_visible_pixels(sky, scale)
        return sky.reshape(-1, 1)

    def sequential_inference(self, sphere, real_vis):
        """

        posterior = to.sequential_inference(prior=prior, real_vis=vis_to_real(disko.vis_arr), sigma_vis=sigma_vis)

        # The image is now at posterior.mu
        sphere.set_visible_pixels(sky, scale)

        """
        gamma = self.make_gamma(sphere)
        n_s = sphere.pixels.shape[0]

        logger.info("Bayesian Inference of sky (n_s = {})".format(n_s))
        t0 = time.time()

        #
        # Create a prior (Using some indication of the expected range of the image)
        #

        p05, p50, p95, p100 = self.vis_stats()
        var = p95 * p95
        logger.info("Sky Prior variance={}".format(var))
        prior = MultivariateGaussian(np.zeros(n_s) + p50, sigma=var * np.identity(n_s))

        #
        # Create a likelihood covariance
        #
        diag = np.diagflat(self.rms ** 2)
        sigma_vis = np.block([[diag, 0.5 * diag], [0.5 * diag, diag]])

        precision = np.linalg.inv(sigma_vis)

        logger.info("y_m = {}".format(real_vis.shape))

        posterior = prior.bayes_update(precision, real_vis, gamma)

        logger.info("Elapsed {}s".format(time.time() - t0))
        return posterior

    def image_tikhonov(self, vis_arr, sphere, alpha, scale=True, usedask=False):
        n_s = sphere.pixels.shape[0]
        n_v = self.u_arr.shape[0]

        print(
            f"image_tikhonov({vis_arr.shape}, {sphere}, {alpha}, scale={scale}, usedask={usedask})"
        )

        lambduh = alpha / np.sqrt(n_s)
        if usedask is False:
            gamma = self.make_gamma(sphere)
            logger.info("augmented: {}".format(gamma.shape))

            vis_aux = vis_to_real(vis_arr)
            logger.info(
                "vis mean: {} shape: {}".format(np.mean(vis_aux), vis_aux.shape)
            )

            tol = min(alpha / 1e4, 1e-10)
            logger.info("Solving tol={} ...".format(tol))

            # reg = linear_model.ElasticNet(alpha=alpha/np.sqrt(n_s),
            # tol=1e-6,
            # l1_ratio = 0.01,
            # max_iter=100000,
            # positive=True)
            if False:
                (
                    sky,
                    lstop,
                    itn,
                    r1norm,
                    r2norm,
                    anorm,
                    acond,
                    arnorm,
                    xnorm,
                    var,
                ) = scipy.sparse.linalg.lsqr(gamma, vis_aux, damp=alpha, show=True)
                logger.info(
                    "Alpha: {}: Iterations: {}: rnorm: {}: xnorm: {}".format(
                        alpha, itn, r2norm, xnorm
                    )
                )
            else:
                reg = linear_model.Ridge(
                    alpha=alpha, tol=tol, solver="lsqr", max_iter=100000
                )

                reg.fit(gamma, vis_aux)
                logger.info("    Solve Complete, iter={}".format(reg.n_iter_))

                sky = reg.coef_ # np.from_array(reg.coef_)

                residual = vis_aux - gamma @ sky

                sky, residual_norm, solution_norm = (
                    sky, np.linalg.norm(residual) ** 2, np.linalg.norm(sky) ** 2
                )

                score = reg.score(gamma, vis_aux)
                logger.info(
                    "Alpha: {}: Loss: {}: rnorm: {}: snorm: {}".format(
                        alpha, score, residual_norm, solution_norm
                    )
                )

        else:
            from dask_ml.linear_model import LinearRegression
            import dask_glm
            from dask.distributed import Client, LocalCluster
            from dask.diagnostics import ProgressBar
            import dask

            logger.info("Starting Dask Client")

            if True:
                cluster = LocalCluster(dashboard_address=":8231", processes=False)
                client = Client(cluster)
            else:
                client = Client("tcp://localhost:8786")

            logger.info("Client = {}".format(client))

            harmonic_list = []
            p2j = 2 * np.pi * 1.0j

            dl = sphere.l
            dm = sphere.m
            dn = sphere.n

            n_arr_minus_1 = dn - 1

            du = self.u_arr
            dv = self.v_arr
            dw = self.w_arr

            for u, v, w in zip(du, dv, dw):
                harmonic = da.from_array(
                    np.exp(p2j * (u * dl + v * dm + w * n_arr_minus_1))
                    / np.sqrt(sphere.npix),
                    chunks=(n_s,),
                )
                harminc = client.persist(harmonic)
                harmonic_list.append(harmonic)

            gamma = da.stack(harmonic_list)
            logger.info("Gamma Shape: {}".format(gamma.shape))
            # gamma = gamma.reshape((n_v, n_s))
            gamma = gamma.conj()
            gamma = client.persist(gamma)

            logger.info("Gamma Shape: {}".format(gamma.shape))

            logger.info("Building Augmented Operator...")
            proj_operator_real = da.real(gamma)
            proj_operator_imag = da.imag(gamma)
            proj_operator = da.block([[proj_operator_real], [proj_operator_imag]])

            proj_operator = client.persist(proj_operator)

            logger.info("Proj Operator shape {}".format(proj_operator.shape))
            vis_aux = da.from_array(
                np.array(
                    np.concatenate((np.real(vis_arr), np.imag(vis_arr))),
                    dtype=np.float32,
                )
            )

            # logger.info("Solving...")

            en = dask_glm.regularizers.ElasticNet(weight=0.01)
            en = dask_glm.regularizers.L2()
            # dT = da.from_array(proj_operator, chunks=(-1, 'auto'))
            ##dT = da.from_array(proj_operator, chunks=(-1, 'auto'))
            # dv = da.from_array(vis_aux)

            dask.config.set({"array.chunk-size": "1024MiB"})
            A = np.rechunk(proj_operator, chunks=("auto", n_s))
            A = client.persist(A)
            y = vis_aux  # np.rechunk(vis_aux, chunks=('auto', n_s))
            y = client.persist(y)
            # sky = dask_glm.algorithms.proximal_grad(A, y, regularizer=en, lambduh=alpha, max_iter=10000)

            logger.info("Rechunking completed.. A= {}.".format(A.shape))
            reg = LinearRegression(
                penalty=en,
                C=1.0 / lambduh,
                fit_intercept=False,
                solver="lbfgs",
                max_iter=1000,
                tol=1e-8,
            )
            sky = reg.fit(A, y)
            sky = reg.coef_
            score = reg.score(proj_operator, vis_aux)
            try:
                logger.info("Loss function: {}".format(score.compute()))
            except:
                logger.info("Loss function: {}".format(score))

        logger.info("Solving Complete: sky = {}".format(sky.shape))

        sphere.set_visible_pixels(sky, scale=False)
        return sky.reshape(-1, 1)

    @classmethod
    def plot(self, plt, sphere, src_list):
        rot = (0, 90, 0)
        plt.figure()  # (figsize=(6,6))
        logger.info("sphere.pixels: {}".format(sphere.pixels.shape))
        if True:
            hp.orthview(
                sphere.pixels, rot=rot, xsize=1000, cbar=True, half_sky=True, hold=True
            )
            hp.graticule(verbose=False)
            plt.tight_layout()
        else:
            hp.mollview(sphere.pixels, rot=rot, xsize=1000, cbar=True)
            hp.graticule(verbose=True)

        if src_list is not None:
            for s in src_list:
                sphere.plot_x(s.el_r, s.az_r)

    def display(self, plt, src_list, nside):
        sphere = HealpixSphere(nside)
        sky = self.solve_vis(self.vis_arr, sphere)
        sphere.plot(plt, src_list)

    def beam(self, plt, nside):
        sphere = HealpixSphere(nside)
        sky = self.solve_vis(np.ones_like(self.vis_arr), nside)
        sphere.plot(plt, src_list=None)
