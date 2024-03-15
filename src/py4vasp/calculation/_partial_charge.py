# Copyright © VASP Software GmbH,
# Licensed under the Apache License 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
import dataclasses
import warnings
from typing import Union

import numpy as np
from scipy.interpolate import CubicSpline
from scipy.ndimage import gaussian_filter, gaussian_filter1d

from py4vasp import exception
from py4vasp._third_party.graph import Graph
from py4vasp._third_party.graph.contour import Contour
from py4vasp._util import select
from py4vasp.calculation import _base, _structure

_STM_MODES = {
    "constant_height": ["constant_height", "ch", "height"],
    "constant_current": ["constant_current", "cc", "current"],
}
_SPINS = ("up", "down", "total")


@dataclasses.dataclass
class STM_settings:
    """Settings for the STM simulation.

    sigma_z : float
        The standard deviation of the Gaussian filter in the z-direction.
        The default is 4.0.
    sigma_xy : float
        The standard deviation of the Gaussian filter in the xy-plane.
        The default is 4.0.
    truncate : float
        The truncation of the Gaussian filter. The default is 3.0.
    enhancement_factor : float
        The enhancement factor for the output of the constant heigth
        STM image. The default is 1000.
    interpolation_factor : int
        The interpolation factor for the z-direction in case of
        constant current mode. The default is 10.
    """

    sigma_z: float = 4.0
    sigma_xy: float = 4.0
    truncate: float = 3.0
    enhancement_factor: float = 1000
    interpolation_factor: int = 10


class PartialCharge(_base.Refinery, _structure.Mixin):
    """Partial charges describe the fraction of the charge density in a certain energy,
    band, or k-point range.

    Partial charges are produced by a post-processing VASP run after self-consistent
    convergence is achieved. They are stored in an array of shape
    (ngxf, ngyf, ngzf, ispin, nbands, nkpts). The first three dimensions are the
    FFT grid dimensions, the fourth dimension is the spin index, the fifth dimension
    is the band index, and the sixth dimension is the k-point index. Both band and
    k-point arrays are also saved and accessible in the .bands() and kpoints() methods.
    If ispin=2, the second spin index is the magnetization density (up-down),
    not the down-spin density.
    Since this is postprocessing data for a fixed density, there are no ionic steps
    to separate the data.
    """

    @property
    def stm_settings(self):
        return STM_settings()

    @_base.data_access
    def __str__(self):
        """Return a string representation of the partial charge density."""
        return f"""
        {"spin polarized" if self._spin_polarized() else ""} partial charge density of {self._topology()}:
        on fine FFT grid: {self.grid()}
        {"summed over all contributing bands" if 0 in self.bands() else f" separated for bands: {self.bands()}"}
        {"summed over all contributing k-points" if 0 in self.kpoints() else f" separated for k-points: {self.kpoints()}"}
        """.strip()

    @_base.data_access
    def grid(self):
        return self._raw_data.grid[:]

    @_base.data_access
    def to_dict(self):
        """Store the partial charges in a dictionary.

        Returns
        -------
        dict
            The dictionary contains the partial charges as well as the structural
            information for reference.
        """

        parchg = np.squeeze(self._raw_data.partial_charge[:].T)
        return {
            "structure": self._structure.read(),
            "grid": self.grid(),
            "bands": self.bands(),
            "kpoints": self.kpoints(),
            "partial_charge": parchg,
        }

    @_base.data_access
    def to_stm(
        self,
        selection: str = "constant_height",
        tip_height: float = 2.0,
        current: float = 1.0,
        supercell: Union[int, np.array] = 2,
    ) -> Graph:
        """Generate stm image data from the partial charge density.

        Parameters
        ----------
        selection : str
            The mode in which the stm is operated and the spin channel to be used.
            Possible modes are "constant_height"(default) and "constant_current".
            Possible spin selections are "total"(default), "up", and "down".
        tip_height : float
            The height of the stm tip above the surface in Angstrom.
            The default is 2.0 Angstrom. Only used in "constant_height" mode.
        current : float
            The tunneling current in nA. The default is 1.
            Only used in "constant_current" mode.
        supercell : int | np.array
            The supercell to be used for plotting the STM. The default is 2.

        Returns
        -------
        Graph
            The STM image as a graph object. The title is the label of the Contour
            object.
        """

        tree = select.Tree.from_selection(selection)
        for index, selection in enumerate(tree.selections()):
            if index > 0:
                message = "Selecting more than one STM is not implemented."
                raise exception.NotImplemented(message)
            contour = self._make_contour(selection, tip_height, current)
        contour.supercell = self._parse_supercell(supercell)
        return Graph(series=contour, title=contour.label)

    def _parse_supercell(self, supercell):
        if isinstance(supercell, int):
            return np.asarray([supercell, supercell])
        if len(supercell) == 2:
            return np.asarray(supercell)
        message = """The supercell has to be a single number or a 2D array. \
        The supercell is used to multiply the x and y directions of the lattice."""
        raise exception.IncorrectUsage(message)

    def _make_contour(self, selection, tip_height, current):
        self._raise_error_if_tip_too_far_away(tip_height)
        mode = self._parse_mode(selection)
        spin = self._parse_spin(selection)
        self._raise_error_if_selection_not_understood(selection, mode, spin)
        smoothed_charge = self._get_stm_data(spin)
        if mode == "constant_height" or mode is None:
            return self._constant_height_stm(smoothed_charge, tip_height, spin)
        current = current * 1e-09  # convert nA to A
        return self._constant_current_stm(smoothed_charge, current, spin)

    def _parse_mode(self, selection):
        for mode, aliases in _STM_MODES.items():
            for alias in aliases:
                if select.contains(selection, alias, ignore_case=True):
                    return mode
        return None

    def _parse_spin(self, selection):
        for spin in _SPINS:
            if select.contains(selection, spin, ignore_case=True):
                return spin
        return None

    def _raise_error_if_selection_not_understood(self, selection, mode, spin):
        if len(selection) != int(mode is not None) + int(spin is not None):
            message = f"STM mode '{selection}' was parsed as mode='{mode}' and spin='{spin}' which could not be used. Please use 'constant_height' or 'constant_current' as mode and 'up', 'down', or 'total' as spin."
            raise exception.IncorrectUsage(message)

    def _constant_current_stm(self, smoothed_charge, current, spin):
        z_start = _min_of_z_charge(
            self._get_stm_data(spin),
            sigma=self.stm_settings.sigma_z,
            truncate=self.stm_settings.truncate,
        )
        grid = self.grid()
        z_step = 1 / self.stm_settings.interpolation_factor
        z_grid = np.arange(z_start, 0, -z_step)
        splines = CubicSpline(range(grid[2]), smoothed_charge, axis=-1)
        scan = z_grid[np.argmax(splines(z_grid) >= current, axis=-1)]
        scan = z_step * scan - self._get_highest_z_coord()
        spin_label = "both spin channels" if spin == "total" else f"spin {spin}"
        topology = self._topology()
        label = f"STM of {topology} for {spin_label} at constant current={current*1e9:.1e} nA"
        return Contour(data=scan, lattice=self._in_plane_vectors(), label=label)

    def _constant_height_stm(self, smoothed_charge, tip_height, spin):
        zz = self._z_index_for_height(tip_height + self._get_highest_z_coord())
        height_scan = smoothed_charge[:, :, zz] * self.stm_settings.enhancement_factor
        spin_label = "both spin channels" if spin == "total" else f"spin {spin}"
        topology = self._topology()
        label = f"STM of {topology} for {spin_label} at constant height={float(tip_height):.2f} Angstrom"
        return Contour(data=height_scan, lattice=self._in_plane_vectors(), label=label)

    def _z_index_for_height(self, tip_height):
        return int(tip_height / self._out_of_plane_vector() * self.grid()[2])

    def _get_highest_z_coord(self):
        return np.max(self._structure.cartesian_positions()[:, 2])

    def _get_lowest_z_coord(self):
        return np.min(self._structure.cartesian_positions()[:, 2])

    def _topology(self):
        return str(self._structure._topology())

    def _estimate_vacuum(self):
        slab_thickness = self._get_highest_z_coord() - self._get_lowest_z_coord()
        return self._out_of_plane_vector() - slab_thickness

    def _raise_error_if_tip_too_far_away(self, tip_height):
        if tip_height > self._estimate_vacuum() / 2:
            message = f"""The tip position at {tip_height:.2f} is above half of the
             estimated vacuum thickness {self._estimate_vacuum():.2f} Angstrom.
            You would be sampling the bottom of your slab, which is not supported."""
            raise exception.IncorrectUsage(message)

    def _get_stm_data(self, spin):
        if 0 not in self.bands() or 0 not in self.kpoints():
            massage = """Simulated STM images are only supported for non-separated bands and k-points.
            Please set LSEPK and LSEPB to .FALSE. in the INCAR file."""
            raise exception.NotImplemented(massage)
        chg = self._correct_units(self.to_numpy(spin, band=0, kpoint=0))
        return self._smooth_stm_data(chg)

    def _correct_units(self, charge_data):
        grid_volume = np.prod(self.grid())
        cell_volume = self._structure.volume()
        return charge_data / (grid_volume * cell_volume)

    def _smooth_stm_data(self, data):
        sigma = (
            self.stm_settings.sigma_xy,
            self.stm_settings.sigma_xy,
            self.stm_settings.sigma_z,
        )
        return gaussian_filter(
            data, sigma=sigma, truncate=self.stm_settings.truncate, mode="wrap"
        )

    def _in_plane_vectors(self):
        """Return the in-plane component of lattice vectors."""
        lattice_vectors = self._structure._lattice_vectors()
        _raise_error_if_3rd_lattice_vector_is_not_parallel_to_z(lattice_vectors)
        return lattice_vectors[:2, :2]

    def _out_of_plane_vector(self):
        """Return out-of-plane component of lattice vectors."""
        lattice_vectors = self._structure._lattice_vectors()
        _raise_error_if_3rd_lattice_vector_is_not_parallel_to_z(lattice_vectors)
        return lattice_vectors[2, 2]

    def _spin_polarized(self):
        return self._raw_data.partial_charge.shape[2] == 2

    @_base.data_access
    def to_numpy(self, selection="total", band=0, kpoint=0):
        """Return the partial charge density as a 3D array.

        Parameters
        ----------
        selection : str
            The spin channel to be used. The default is "total".
            The other options are "up" and "down".
        band : int
            The band index. The default is 0, which means that all bands are summed.
        kpoint : int
            The k-point index. The default is 0, which means that all k-points are summed.

        Returns
        -------
        np.array
            The partial charge density as a 3D array.
        """

        band = self._check_band_index(band)
        kpoint = self._check_kpoint_index(kpoint)

        parchg = self._raw_data.partial_charge[:].T
        if not self._spin_polarized() or selection == "total":
            return parchg[:, :, :, 0, band, kpoint]
        if selection == "up":
            return parchg[:, :, :, :, band, kpoint] @ np.array([0.5, 0.5])
        if selection == "down":
            return parchg[:, :, :, :, band, kpoint] @ np.array([0.5, -0.5])

        message = f"Spin '{selection}' not understood. Use 'up', 'down' or 'total'."
        raise exception.IncorrectUsage(message)

    @_base.data_access
    def bands(self):
        """Return the band array listing the contributing bands.

        [2,4,5] means that the 2nd, 4th, and 5th bands are contributing while
        [0] means that all bands are contributing.
        """

        return self._raw_data.bands[:]

    def _check_band_index(self, band):
        bands = self.bands()
        if band in bands:
            return np.where(bands == band)[0][0]
        elif 0 in bands:
            message = f"""The band index {band} is not available.
            The summed partial charge density is returned instead."""
            warnings.warn(message, UserWarning)
            return 0
        else:
            message = f"""Band {band} not found in the bands array.
            Make sure to set IBAND, EINT, and LSEPB correctly in the INCAR file."""
            raise exception.NoData(message)

    @_base.data_access
    def kpoints(self):
        """Return the k-points array listing the contributing k-points.

        [2,4,5] means that the 2nd, 4th, and 5th k-points are contributing with
        all weights = 1. [0] means that all k-points are contributing.
        """
        return self._raw_data.kpoints[:]

    def _check_kpoint_index(self, kpoint):
        kpoints = self.kpoints()
        if kpoint in kpoints:
            return np.where(kpoints == kpoint)[0][0]
        elif 0 in kpoints:
            message = f"""The k-point index {kpoint} is not available.
            The summed partial charge density is returned instead."""
            warnings.warn(message, UserWarning)
            return 0
        else:
            message = f"""K-point {kpoint} not found in the kpoints array.
            Make sure to set KPUSE and LSEPK correctly in the INCAR file."""
            raise exception.NoData(message)


def _raise_error_if_3rd_lattice_vector_is_not_parallel_to_z(lattice_vectors):
    lv = lattice_vectors
    if lv[0][2] != 0 or lv[1][2] != 0 or lv[2][0] != 0 or lv[2][1] != 0:
        message = """The third lattice vector is not in cartesian z-direction.
        or the first two lattice vectors are not in the xy-plane.
        STM simulations for such cells are not implemented."""
        raise exception.NotImplemented(message)


def _min_of_z_charge(charge, sigma=4, truncate=3.0):
    """Returns the z-coordinate of the minimum of the charge density in the z-direction"""
    # average over the x and y axis
    z_charge = np.mean(charge, axis=(0, 1))
    # smooth the data using a gaussian filter
    z_charge = gaussian_filter1d(z_charge, sigma=sigma, truncate=truncate, mode="wrap")
    # return the z-coordinate of the minimum
    return np.argmin(z_charge)
