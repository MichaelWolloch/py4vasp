import dataclasses
import warnings

import numpy as np
from scipy.interpolate import CubicSpline
from scipy.ndimage import gaussian_filter, gaussian_filter1d

from py4vasp.calculation import _base, _structure

stm_modes = {
    "constant_height": ["constant_height", "ch", "constant height"],
    "constant_current": [
        "constant_current",
        "cc",
        "constant current",
    ],
}


class PartialCharge(_base.Refinery, _structure.Mixin):
    """
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

    def to_dict(self, squeeze=True):
        """Store the partial charges in a dictionary.

        Returns
        -------
        dict
            The dictionary contains the partial charges as well as the structural
            information for reference.
        """

        return {
            **self._read_structure(),
            **self._read_grid(),
            **self._read_bands(),
            **self._read_kpoints(),
            **self._read_partial_charge(squeeze=squeeze),
        }

    def to_stm(
        self,
        mode="constant_height",
        tip_height=2.0,
        current=1e-9,
        spin="both",
        **kwargs,
    ):
        """Generate STM image data from the partial charge density.

        Parameters
        ----------
        mode : str
            The mode in which the STM is operated. The default is "constant_height".
            The other option is "constant_current".
        tip_height : float
            The height of the STM tip above the surface in Angstrom.
            The default is 2.0 Angstrom. Only used in "constant_height" mode.
        current : float
            The tunneling current in A. The default is 1e-9.
            Only used in "constant_current" mode.
        spin : str
            The spin channel to be used. The default is "both".
            The other options are "up" and "down".
        kwargs
            Additional keyword arguments are passed to the STM calculation.
            Specifically, the following parameters can be set:
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


        Returns
        -------
        STM
            The STM object contains the data to plot an image as well as the lattice vectors in the xy-plane and a label.
        """

        default_params = {
            "sigma_z": 4.0,
            "sigma_xy": 4.0,
            "truncate": 3.0,
            "enhancement_factor": 1000,
            "interpolation_factor": 10,
        }

        self._check_z_orth()
        if mode.lower() in stm_modes["constant_height"]:
            self.tip_height = tip_height
            self._check_tip_height()

        default_params.update(kwargs)
        self.sigma_z = default_params["sigma_z"]
        self.sigma_xy = default_params["sigma_xy"]
        self.truncate = default_params["truncate"]
        self.enhancement_factor = default_params["enhancement_factor"]
        self.interpolation_factor = default_params["interpolation_factor"]

        self.smoothed_charge = self._get_stm_data(spin)

        if mode.lower() in stm_modes["constant_height"]:
            self.STM = self._constant_height_stm(tip_height, spin)
            return self.STM
        elif mode.lower() in stm_modes["constant_current"]:
            self.STM = self._constant_current_stm(current, spin)
            return self.STM
        else:
            raise ValueError(
                f"STM mode '{mode}' not understood. Use 'constant_height' or 'constant_current'."
            )

    def plot_STM(self, **kwargs):
        """Plot the STM image.

        If the STM is not calculated yet, a ValueError is raised.
        """

        # check if STM is calculated already
        if getattr(self, "STM", None) is None:
            raise ValueError("STM is not calculated yet. Please calculate STM first.")
        plot_scan(self.STM, **kwargs)

    def _constant_current_stm(self, current, spin):
        z_start = min_of_z_charge(
            self._get_stm_data(spin), sigma=self.sigma_z, truncate=self.truncate
        )
        grid = self.grid()
        cc_scan = np.zeros((grid[0], grid[1]))
        # scan over the x and y grid
        for i in range(grid[0]):
            for j in range(grid[1]):
                # for more accuracy, interpolate each z-line of data with cubic splines
                spl = CubicSpline(range(grid[2]), self.smoothed_charge[i][j])

                for k in np.arange(z_start, 0, -1 / self.interpolation_factor):
                    if spl(k) >= current:
                        break
                cc_scan[i][j] = k
        # normalize the scan
        cc_scan = cc_scan - np.min(cc_scan.flatten())
        spin_label = "both spin channels" if spin == "both" else f"spin {spin}"
        topology = self._topology()
        label = (
            f"STM of {topology} for {spin_label} at constant current={current:.1e} A"
        )
        return STM_data(data=cc_scan, lattice=self.lattice_vectors()[:2], label=label)

    def _constant_height_stm(self, tip_height, spin):
        grid = self.grid()
        z_index = self._z_index_for_height(
            self.tip_height + self._get_highest_z_coord()
        )
        ch_scan = np.zeros((grid[0], grid[1]))
        for i in range(grid[0]):
            for j in range(grid[1]):
                ch_scan[i][j] = (
                    self.smoothed_charge[i][j][z_index] * self.enhancement_factor
                )
        spin_label = "both spin channels" if spin == "both" else f"spin {spin}"
        topology = self._topology()
        label = f"STM of {topology} for {spin_label} at constant height={float(self.tip_height):.2f} Angstrom"
        return STM_data(
            data=ch_scan,
            lattice=self.lattice_vectors()[:2],
            label=label,
        )

    def _z_index_for_height(self, tip_height):
        return int(tip_height / self.lattice_vectors()[2][2] * self.grid()[2])

    @_base.data_access
    def _get_highest_z_coord(self):
        return np.max(self._structure.cartesian_positions()[:, 2])

    @_base.data_access
    def _get_lowest_z_coord(self):
        return np.min(self._structure.cartesian_positions()[:, 2])

    @_base.data_access
    def _topology(self):
        return str(self._structure._topology())

    def _estimate_vacuum(self):
        slab_thickness = self._get_highest_z_coord() - self._get_lowest_z_coord()
        z_vector = self.lattice_vectors()[2, 2]
        return z_vector - slab_thickness

    def _check_tip_height(self):
        if self.tip_height > self._estimate_vacuum() / 2:
            message = f"""The tip position at {self.tip_height:.2f} is above half of the
             estimated vacuum thickness {self._estimate_vacuum():.2f} Angstrom.
            You would be sampling the bottom of your slab, which is not supported."""
            raise ValueError(message)

    def _check_z_orth(self):
        lv = self.lattice_vectors()
        if lv[0][2] != 0 or lv[1][2] != 0 or lv[2][0] != 0 or lv[2][1] != 0:
            message = """The third lattice vector is not in cartesian z-direction.
            or the first two lattice vectors are not in the xy-plane.
            The STM calculation is not supported."""
            raise ValueError(message)

    def _get_stm_data(self, spin):
        if 0 not in self.bands() or 0 not in self.kpoints():
            massage = """Simulated STM images are only supported for non-separated bands and k-points.
            Please set LSEPK and LSEPB to .FALSE. in the INCAR file."""
            raise ValueError(massage)
        chg = self._correct_units(self.to_array(band=0, kpoint=0, spin=spin))
        return self._smooth_stm_data(chg)

    @_base.data_access
    def _correct_units(self, charge_data):
        grid_volume = np.prod(self.grid())
        cell_volume = self._structure.volume()
        return charge_data / (grid_volume * cell_volume)

    def _smooth_stm_data(self, data):
        smoothed_charge = gaussian_filter(
            data,
            sigma=(self.sigma_xy, self.sigma_xy, self.sigma_z),
            truncate=self.truncate,
            mode="wrap",
        )
        return smoothed_charge

    @_base.data_access
    def lattice_vectors(self):
        """Return the lattice vectors of the input structure."""
        return self._structure._lattice_vectors()

    def _spin_polarized(self):
        return self._raw_data.partial_charge.shape[2] == 2

    def _read_grid(self):
        return {"grid": self.grid()}

    def _read_bands(self):
        return {"bands": self.bands()}

    def _read_kpoints(self):
        return {"kpoints": self.kpoints()}

    @_base.data_access
    def _read_structure(self):
        return {"structure": self._structure.read()}

    @_base.data_access
    def _read_partial_charge(self, squeeze=True):
        if squeeze:
            return {"partial_charge": np.squeeze(self._raw_data.partial_charge[:].T)}
        else:
            return {"partial_charge": self._raw_data.partial_charge[:].T}

    @_base.data_access
    def to_array(self, band=0, kpoint=0, spin="both"):
        """Return the partial charge density as a 3D array.

        Parameters
        ----------
        band : int
            The band index. The default is 0, which means that all bands are summed.
        kpoint : int
            The k-point index. The default is 0, which means that all k-points are summed.
        spin : str
            The spin channel to be used. The default is "both".
            The other options are "up" and "down".

        Returns
        -------
        np.array
            The partial charge density as a 3D array.
        """

        parchg = self._raw_data.partial_charge[:].T

        band = self._check_band_index(band)
        kpoint = self._check_kpoint_index(kpoint)

        if self._spin_polarized():
            if spin == "both":
                parchg = parchg[:, :, :, 0, band, kpoint]
            elif spin == "up":
                parchg = (
                    parchg[:, :, :, 0, band, kpoint] + parchg[:, :, :, 1, band, kpoint]
                ) / 2
            elif spin == "down":
                parchg = (
                    parchg[:, :, :, 0, band, kpoint] - parchg[:, :, :, 1, band, kpoint]
                ) / 2
            else:
                raise ValueError(
                    f"Spin '{spin}' not understood. Use 'up', 'down' or 'both'."
                )
        else:
            parchg = parchg[:, :, :, 0, band, kpoint]

        return parchg

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
            raise ValueError(f"Band {band} not found in the bands array.")

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
            raise ValueError(f"K-point {kpoint} not found in the kpoints array.")


@dataclasses.dataclass
class STM_data:
    """
    STM data is stored in a 2D array with the same dimensions as the FFT grid.
    Other information such as the mode, the tip height, the current, spin-channel,
    and lattice vectors in x and y are stored as well.
    """

    data: np.array
    lattice: np.array
    label: str


def min_of_z_charge(
    charge,
    sigma=4,
    truncate=3.0,
):
    """Returns the z-coordinate of the minimum of the charge density in the z-direction"""
    # average over the x and y axis
    z_charge = np.mean(charge, axis=(0, 1))
    # smooth the data using a gaussian filter
    z_charge = gaussian_filter1d(z_charge, sigma=sigma, truncate=truncate, mode="wrap")
    # return the z-coordinate of the minimum
    return np.argmin(z_charge)


def plot_scan(
    stm_data,
    mult_xy=[2, 2],
    levels=40,
    cmap="copper",
    name="STM",
):
    """
    Function to plot the STM image to a file.

    The file is named scan_cc or scan_ch.png, depending on mode.

    A contour plot is used with 20 levels. The xy-unit cell is also plotted.

    """

    grid = stm_data.data.shape
    # make the xy-grid in cartesian coordinates
    XX, YY = make_cart_grid(grid, stm_data.lattice, mult_xy)
    # multiply the image in the x and y directions
    scan = multiply_image(stm_data.data, [mult_xy[1], mult_xy[0]])
    # plot the STM image
    import matplotlib.pyplot as plt

    plt.contourf(XX, YY, scan.T, levels, cmap=cmap)
    plt.colorbar()
    # use the 2D lattice vectors to plot the xy-unit cell
    lattice = stm_data.lattice
    plt.plot([0, lattice[0, 0]], [0, lattice[0, 1]], "k-", linewidth=2)
    plt.plot([0, lattice[1, 0]], [0, lattice[1, 1]], "k-", linewidth=2)
    plt.plot(
        [lattice[0, 0], lattice[0, 0] + lattice[1, 0]],
        [lattice[0, 1], lattice[0, 1] + lattice[1, 1]],
        "k-",
        linewidth=2,
    )
    plt.plot(
        [lattice[1, 0], lattice[0, 0] + lattice[1, 0]],
        [lattice[1, 1], lattice[0, 1] + lattice[1, 1]],
        "k-",
        linewidth=2,
    )
    plt.axis("equal")
    plt.axis("off")
    plt.title(stm_data.label)
    if "constant current" in stm_data.label:
        plt.savefig(f"{name}_constant_current.png", dpi=300)
    else:
        plt.savefig(f"{name}_constant_height.png", dpi=300)

    plt.show()
    plt.clf()
    return


def make_cart_grid(grid, lattice, mult):
    """Function to convert the grid points to cartesian coordinates and create a meshgrid"""
    if len(mult) == 2:
        grid = (grid[0] * mult[0], grid[1] * mult[1])
        lattice = lattice[:2, :2]
        # make meshgrid
        x = np.linspace(0, mult[0], grid[0])
        y = np.linspace(0, mult[1], grid[1])
        XX, YY = np.meshgrid(x, y)
        # convert to cartesian coordinates
        coordinates = np.dot(np.column_stack((XX.flatten(), YY.flatten())), lattice)
        # reshape the coordinates to the shape of the meshgrid
        XX = np.reshape(coordinates[:, 0], XX.shape)
        YY = np.reshape(coordinates[:, 1], YY.shape)
        return XX, YY
    elif len(mult) == 3:
        grid = (grid[0] * mult[0], grid[1] * mult[1], grid[2] * mult[2])
        # make meshgrid
        x = np.linspace(0, mult[0], grid[0])
        y = np.linspace(0, mult[1], grid[1])
        z = np.linspace(0, mult[2], grid[2])
        XX, YY, ZZ = np.meshgrid(x, y, z)
        # convert to cartesian coordinates
        coordinates = np.dot(
            np.column_stack((XX.flatten(), YY.flatten(), ZZ.flatten())), lattice
        )
        # reshape the coordinates to the shape of the meshgrid
        XX = np.reshape(coordinates[:, 0], XX.shape)
        YY = np.reshape(coordinates[:, 1], YY.shape)
        ZZ = np.reshape(coordinates[:, 2], ZZ.shape)
        return XX, YY, ZZ


def multiply_image(scan, mult_xy):
    """Function to multiply the image in the x and y directions"""
    scan = np.tile(scan, (mult_xy[1], mult_xy[0]))
    return scan
