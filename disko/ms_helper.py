import dask
import distributed
import logging
import datetime
import time

import numpy as np

from daskms import xds_from_table, xds_from_ms, xds_to_table, TableProxy

from dask.diagnostics import ProgressBar
from dask.distributed import progress

from .resolution import Resolution

logger = logging.getLogger(__name__)
logger.addHandler(
    logging.NullHandler()
)  # Add other handlers if you're using this as a library
logger.setLevel(logging.INFO)



class RadioObservation(object):
    def __init__(self):
        pass


def read_ms(ms, num_vis, angular_resolution, chunks=1000, channel=0, field_id=0):
    """
    Use dask-ms to load the necessary data to create a telescope operator
    (will use uvw positions, and antenna positions)

    -- angular_resolution: Used to calculate the maximum baselines to consider.
                   We want two pixels per smallest fringe
                   pix_res > fringe / 2

                   u sin(theta) = n (for nth fringe)
                   at small angles: theta = 1/u, or bl_max = 1 / theta

                   d sin(theta) = lambda / 2
                   d / lambda = 1 / (2 sin(theta))
                   bl_max = lambda / 2sin(theta)


    """

    # local_cluster = distributed.LocalCluster(processes=False)
    # address = local_cluster.scheduler_address
    # logging.info("Using distributed scheduler "
    # "with address '{}'".format(address))
    # client = distributed.Client()

    try:
        # Create a dataset representing the entire antenna table
        ant_table = "::".join((ms, "ANTENNA"))

        for ant_ds in xds_from_table(ant_table):
            # print(ant_ds)
            # print(dask.compute(ant_ds.NAME.data,
            # ant_ds.POSITION.data,
            # ant_ds.DISH_DIAMETER.data))
            ant_p = np.array(ant_ds.POSITION.data)
        logger.info("Antenna Positions {}".format(ant_p.shape))

        # Create a dataset representing the field
        field_table = "::".join((ms, "FIELD"))
        for field_ds in xds_from_table(field_table):
            phase_dir = np.array(field_ds.PHASE_DIR.data)[0].flatten()
            name = field_ds.NAME.data.compute()
            logger.info("Field {}: Phase Dir {}".format(name, np.degrees(phase_dir)))

        # Create datasets representing each row of the spw table
        spw_table = "::".join((ms, "SPECTRAL_WINDOW"))

        for spw_ds in xds_from_table(spw_table, group_cols="__row__"):
            logger.info("CHAN_FREQ.values: {}".format(spw_ds.CHAN_FREQ.values.shape))
            frequencies = dask.compute(spw_ds.CHAN_FREQ.values)[0].flatten()
            frequency = frequencies[channel]
            logger.info("Frequencies = {}".format(frequencies))
            logger.info("Frequency = {}".format(frequency))
            logger.info("NUM_CHAN = %f" % np.array(spw_ds.NUM_CHAN.values)[0])

        # Create datasets from a partioning of the MS
        datasets = list(xds_from_ms(ms, chunks={"row": chunks}))
        logger.info("DataSets: N={}".format(len(datasets)))

        pol = 0

        def read_np_array(da, title, dtype=np.float32):
            tic = time.perf_counter()
            logger.info("Reading {}...".format(title))
            ret = np.array(da, dtype=dtype)
            toc = time.perf_counter()
            logger.info("Elapsed {:04f} seconds".format(toc - tic))
            return ret

        for i, ds in enumerate(datasets):
            logger.info(
                "DATASET field_id={} shape: {}".format(ds.FIELD_ID, ds.DATA.data.shape)
            )
            logger.info("UVW shape: {}".format(ds.UVW.data.shape))
            logger.info("SIGMA shape: {}".format(ds.SIGMA.data.shape))
            if int(field_id) == int(ds.FIELD_ID):
                uvw = read_np_array(ds.UVW.data, "UVW")
                flags = read_np_array(
                    ds.FLAG.data[:, channel, pol], "FLAGS", dtype=np.int32
                )

                #
                #
                #   Now calculate which indices we should use to get the required number of
                #   visibilities.
                #
                bl_max = angular_resolution.get_min_baseline(frequency)

                logger.info("Resolution Max UVW: {:g} meters".format(bl_max))
                logger.info("Flags: {}".format(flags.shape))

                # Now report the recommended resolution from the data.
                # 1.0 / 2*np.sin(theta) = limit_u
                limit_uvw = np.max(np.abs(uvw), 0)

                res_limit = Resolution.from_baseline(np.max(limit_uvw), frequency)
                logger.info(f"Nyquist resolution: {res_limit}")

                if True:
                    bl = np.sqrt(uvw[:, 0] ** 2 + uvw[:, 1] ** 2 + uvw[:, 2] ** 2)
                    # good_data = np.array(np.where((flags == 0) & (np.max(np.abs(uvw), 1) < bl_max))).T.reshape((-1,))
                    good_data = np.array(
                        np.where((flags == 0) & (bl < bl_max))
                    ).T.reshape((-1,))
                else:
                    good_data = np.array(np.where(flags == 0)).T.reshape((-1,))
                logger.info("Good Data {}".format(good_data.shape))

                logger.info("Maximum UVW: {}".format(limit_uvw))
                logger.info("Minimum UVW: {}".format(np.min(np.abs(uvw), 0)))

                for i in range(3):
                    p05, p50, p95 = np.percentile(np.abs(uvw[:, i]), [5, 50, 95])
                    logger.info(
                        "       U[{}]: {:5.2f} {:5.2f} {:5.2f}".format(i, p05, p50, p95)
                    )

                n_ant = len(ant_p)

                n_max = len(good_data)

                if n_max <= num_vis:
                    indices = good_data
                else:
                    indices = np.random.choice(
                        good_data, min(num_vis, n_max), replace=False
                    )
                    # sort the indices to keep them in order (speeds up IO)
                    indices = np.sort(indices)
                #
                #
                #   Now read the remaining data
                #
                sigma = read_np_array(ds.SIGMA.data[indices, pol], "SIGMA")
                # ant1   = read_np_array(ds.ANTENNA1.data[indices], "ANTENNA1")
                # ant12  = read_np_array(ds.ANTENNA1.data[indices], "ANTENNA2")
                cv_vis = read_np_array(
                    ds.DATA.data[indices, channel, pol], "DATA", dtype=np.complex64
                )

                epoch_seconds = np.array(ds.TIME.data)[0]

        if "uvw" not in locals():
            raise RuntimeError("FIELD_ID ({}) is invalid".format(field_id))

        hdr = {
            "CTYPE1": ("RA---SIN", "Right ascension angle cosine"),
            "CRVAL1": np.degrees(phase_dir)[0],
            "CUNIT1": "deg     ",
            "CTYPE2": ("DEC--SIN", "Declination angle cosine "),
            "CRVAL2": np.degrees(phase_dir)[1],
            "CUNIT2": "deg     ",
            "CTYPE3": "FREQ    ",  #           / Central frequency  ",
            "CRPIX3": 1.0,
            "CRVAL3": "{}".format(frequency),
            "CDELT3": 10026896.158854,
            "CUNIT3": "Hz      ",
            "EQUINOX": "2000.",
            "DATE-OBS": "{}".format(epoch_seconds),
            "BTYPE": "Intensity",
        }

        # from astropy.wcs.utils import celestial_frame_to_wcs
        # from astropy.coordinates import FK5
        # frame = FK5(equinox='J2010')
        # wcs = celestial_frame_to_wcs(frame)
        # wcs.to_header()

        u_arr = uvw[indices, 0].T
        v_arr = uvw[indices, 1].T
        w_arr = uvw[indices, 2].T

        rms_arr = sigma.T

        logger.info("Max vis {}".format(np.max(np.abs(cv_vis))))

        # Convert from reduced Julian Date to timestamp.
        timestamp = datetime.datetime(
            1858, 11, 17, 0, 0, 0, tzinfo=datetime.timezone.utc
        ) + datetime.timedelta(seconds=epoch_seconds)

    except Exception as e:
        logger.info("Exception {}".format(e))
        logger.exception(e)

    # finally:
    # client.close()
    # local_cluster.close()

    return u_arr, v_arr, w_arr, frequency, cv_vis, hdr, timestamp, rms_arr, indices
