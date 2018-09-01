"""
Vector gridding using elasticity Green's functions from Sandwell and Wessel
(2016).
"""
import numpy as np
from sklearn.utils.validation import check_is_fitted

from .base import check_fit_input, least_squares, BaseGridder
from .spline import warn_weighted_exact_solution
from .utils import n_1d_arrays, parse_engine
from .coordinates import get_region

try:
    import numba
    from numba import jit
except ImportError:
    numba = None
    from .utils import dummy_jit as jit


class VectorSpline2D(BaseGridder):
    r"""
    Elastically coupled interpolation of 2-component vector data.

    This gridder assumes Cartesian coordinates.

    Uses the Green's functions based on elastic deformation from [SandwellWessel2016]_.
    The interpolation is done by estimating point forces that generate an elastic
    deformation that fits the observed vector data. The deformation equations are based
    on a 2D elastic sheet with a constant Poisson's ratio. The data can then be
    predicted at any desired location.

    The east and north data components are coupled through the elastic deformation
    equations. This coupling is controlled by the Poisson's ratio, which is usually
    between -1 and 1. The special case of Poisson's ratio -1 leads to an uncoupled
    interpolation, meaning that the east and north components don't interfere with each
    other.

    The point forces are traditionally placed under each data point. The force locations
    are set the first time :meth:`~verde.VectorSpline2D.fit` is called. Subsequent calls
    will fit using the same force locations as the first call. This configuration
    results in an exact prediction at the data points but can be unstable.

    [SandwellWessel2016]_ stabilize the solution using Singular Value Decomposition but
    we use ridge regression instead. The regularization can be controlled using the
    *damping* argument. Alternatively, you can specify the position of the forces
    manually using the *force_coords* argument. Regularization or forces not coinciding
    with data points will result in a least-squares estimate, not an exact solution.
    Note that the least-squares solution is required for data weights to have any
    effect.

    Before fitting, the Jacobian (design, sensitivity, feature, etc) matrix for the
    spline is normalized using :class:`sklearn.preprocessing.StandardScaler` without
    centering the mean so that the transformation can be undone in the estimated forces.

    Parameters
    ----------
    poisson : float
        The Poisson's ratio for the elastic deformation Green's functions. Default is
        0.5. A value of -1 will lead to uncoupled interpolation of the east and north
        data components.
    mindist : float
        A minimum distance between the point forces and data points. Needed because the
        Green's functions are singular when forces and data points coincide. Acts as a
        fudge factor. A good rule of thumb is to use the average spacing between data
        points.
    damping : None or float
        The positive damping regularization parameter. Controls how much smoothness is
        imposed on the estimated forces. If None, no regularization is used.
    force_coords : None or tuple of arrays
        The easting and northing coordinates of the point forces. If None (default),
        then will be set to the data coordinates the first time
        :meth:`~verde.VectorSpline2D.fit` is called.
    engine : str
        Computation engine for the Jacobian matrix. Can be ``'auto'``, ``'numba'``, or
        ``'numpy'``. If ``'auto'``, will use numba if it is installed or numpy
        otherwise. The numba version is multi-threaded and considerably faster, which
        makes fitting and predicting faster.

    Attributes
    ----------
    forces_ : array
        The estimated forces that fit the observed data.
    region_ : tuple
        The boundaries (``[W, E, S, N]``) of the data used to fit the
        interpolator. Used as the default region for the
        :meth:`~verde.VectorSpline2D.grid` and :meth:`~verde.VectorSpline2D.scatter`
        methods.

    """

    def __init__(
        self, poisson=0.5, mindist=10e3, damping=None, force_coords=None, engine="auto"
    ):
        self.poisson = poisson
        self.mindist = mindist
        self.damping = damping
        self.force_coords = force_coords
        self.engine = engine

    def fit(self, coordinates, data, weights=None):
        """
        Fit the gridder to the given 2-component vector data.

        The data region is captured and used as default for the
        :meth:`~verde.VectorSpline2D.grid` and :meth:`~verde.VectorSpline2D.scatter`
        methods.

        All input arrays must have the same shape.

        Parameters
        ----------
        coordinates : tuple of arrays
            Arrays with the coordinates of each data point. Should be in the
            following order: (easting, northing, vertical, ...). Only easting
            and northing will be used, all subsequent coordinates will be
            ignored.
        data : tuple of array
            A tuple ``(east_component, north_component)`` of arrays with the
            vector data values at each point.
        weights : None or tuple array
            If not None, then the weights assigned to each data point. Must be
            one array per data component. Typically, this should be 1 over the
            data uncertainty squared.

        Returns
        -------
        self
            Returns this estimator instance for chaining operations.

        """
        coordinates, data, weights = check_fit_input(
            coordinates, data, weights, unpack=False
        )
        if len(data) != 2:
            raise ValueError(
                "Need two data components. Only {} given.".format(len(data))
            )
        # Capture the data region to use as a default when gridding.
        self.region_ = get_region(coordinates[:2])
        if any(w is not None for w in weights):
            weights = np.concatenate([i.ravel() for i in weights])
        else:
            weights = None
        warn_weighted_exact_solution(self, weights)
        data = np.concatenate([i.ravel() for i in data])
        if self.force_coords is None:
            self.force_coords = tuple(i.copy() for i in n_1d_arrays(coordinates, n=2))
        jacobian = self.jacobian(coordinates[:2], self.force_coords)
        self.force_ = least_squares(jacobian, data, weights, self.damping)
        return self

    def predict(self, coordinates):
        """
        Evaluate the fitted gridder on the given set of points.

        Requires a fitted estimator (see :meth:`~verde.VectorSpline2D.fit`).

        Parameters
        ----------
        coordinates : tuple of arrays
            Arrays with the coordinates of each data point. Should be in the
            following order: (easting, northing, vertical, ...). Only easting
            and northing will be used, all subsequent coordinates will be
            ignored.

        Returns
        -------
        data : tuple of arrays
            A tuple ``(east_component, north_component)`` of arrays with the
            predicted vector data values at each point.

        """
        check_is_fitted(self, ["force_"])
        jac = self.jacobian(coordinates[:2], self.force_coords)
        cast = np.broadcast(*coordinates[:2])
        npoints = cast.size
        components = jac.dot(self.force_).reshape((2, npoints))
        return tuple(comp.reshape(cast.shape) for comp in components)

    def jacobian(self, coordinates, force_coords, dtype="float64"):
        """
        Make the Jacobian matrix for the 2D coupled elastic deformation.

        The Jacobian is segmented into 4 parts, each relating a force component to a
        data component [SandwellWessel2016]_::

            | J_ee  J_ne |*|f_e| = |d_e|
            | J_ne  J_nn | |f_n|   |d_n|

        The forces and data are assumed to be stacked into 1D arrays with the east
        component on top of the north component.

        Parameters
        ----------
        coordinates : tuple of arrays
            Arrays with the coordinates of each data point. Should be in the
            following order: (easting, northing, vertical, ...). Only easting and
            northing will be used, all subsequent coordinates will be ignored.
        force_coords : tuple of arrays
            Arrays with the coordinates for the forces. Should be in the same order as
            the coordinate arrays.
        dtype : str or numpy dtype
            The type of the Jacobian array.

        Returns
        -------
        jacobian : 2D array
            The (n_data*2, n_forces*2) Jacobian matrix.

        """
        force_east, force_north = n_1d_arrays(force_coords, n=2)
        east, north = n_1d_arrays(coordinates, n=2)
        if parse_engine(self.engine) == "numba":
            jac = jacobian_numba(
                east,
                north,
                force_east,
                force_north,
                self.mindist,
                self.poisson,
                np.empty((east.size * 2, force_east.size * 2), dtype=dtype),
            )
        else:
            jac = jacobian_numpy(
                east, north, force_east, force_north, self.mindist, self.poisson, dtype
            )
        return jac


def jacobian_numpy(east, north, force_east, force_north, mindist, poisson, dtype):
    """
    Calculate the Jacobian matrix using numpy broadcasting.
    """
    npoints = east.size
    nforces = force_east.size
    # Reshaping the data coordinates to a column vector will automatically build a
    # distance matrix between each data point and force.
    east_orig = east.reshape((npoints, 1)) - force_east
    north_orig = north.reshape((npoints, 1)) - force_north
    distance = np.hypot(east_orig, north_orig, dtype=dtype)
    # The mindist factor helps avoid singular matrices when the force and
    # computation point are too close
    distance += mindist
    # Pre-compute common terms for the Green's functions of each component
    ln_r = (3 - poisson) * np.log(distance)
    over_r2 = (1 + poisson) / distance ** 2
    jac = np.empty((npoints * 2, nforces * 2), dtype=dtype)
    jac[:npoints, :nforces] = ln_r + over_r2 * north_orig ** 2  # J_ee
    jac[npoints:, nforces:] = ln_r + over_r2 * east_orig ** 2  # J_nn
    jac[:npoints, nforces:] = -over_r2 * east_orig * north_orig  # J_ne
    jac[npoints:, :nforces] = jac[:npoints, nforces:]  # J is symmetric
    return jac


@jit(nopython=True, target="cpu", fastmath=True, parallel=True)
def jacobian_numba(east, north, force_east, force_north, mindist, poisson, jac):
    """
    Calculate the Jacobian matrix using numba to speed things up.
    """
    # pylint: disable=too-many-locals
    nforces = force_east.size
    npoints = east.size
    for i in numba.prange(npoints):  # pylint: disable=not-an-iterable
        for j in range(nforces):
            east_orig = east[i] - force_east[j]
            north_orig = north[i] - force_north[j]
            distance = np.sqrt(east_orig ** 2 + north_orig ** 2)
            distance += mindist
            # Pre-compute common terms for the Green's functions of each component
            ln_r = (3 - poisson) * np.log(distance)
            over_r2 = (1 + poisson) / distance ** 2
            jac[i, j] = ln_r + over_r2 * north_orig ** 2  # J_ee
            jac[i + npoints, j + nforces] = ln_r + over_r2 * east_orig ** 2  # J_nn
            jac[i, j + nforces] = -over_r2 * east_orig * north_orig  # J_ne
            jac[i + npoints, j] = jac[i, j + nforces]  # J is symmetric
    return jac
