from py4vasp.exceptions import RefinementException
from typing import NamedTuple
import nglview
import numpy as np


class _Arrow3d(NamedTuple):
    tail: np.ndarray
    tip: np.ndarray
    color: np.ndarray
    radius: float = 0.2

    def to_serializable(self):
        return list(self.tail), list(self.tip), list(self.color), float(self.radius)


_x_axis = _Arrow3d(tail=np.zeros(3), tip=np.array((3, 0, 0)), color=[1, 0, 0])
_y_axis = _Arrow3d(tail=np.zeros(3), tip=np.array((0, 3, 0)), color=[0, 1, 0])
_z_axis = _Arrow3d(tail=np.zeros(3), tip=np.array((0, 0, 3)), color=[0, 0, 1])


class Viewer3d:
    """Collection of data and elements to be displayed in a structure viewer.

    Parameters
    ----------
    viewer : nglview.NGLWidget
        The raw viewer used to display the structure. Currently we are only
        supporting the nglview package.
    """

    _positions = None
    _multiple_cells = 1
    _axes = None
    _arrows = []

    def __init__(self, viewer):
        self._ngl = viewer

    @classmethod
    def from_structure(cls, structure, supercell=None):
        """ Generate a new Viewer3d from a structure.

        Parameters
        ----------
        structure : data.Structure
            Defines the structure of the Vasp calculation.
        supercell : int or np.ndarray
            If present the cell is extended by the specified factor along each axis.
        """
        ase = structure.to_ase(supercell)
        res = cls(nglview.show_ase(ase))
        res._positions = ase.positions
        if supercell is not None:
            res._multiple_cells = np.prod(supercell)
        return res

    def _ipython_display_(self):
        self._ngl._ipython_display_()

    def show_cell(self):
        """ Show the unit cell of the crystal. """
        self._ngl.add_unitcell()

    def hide_cell(self):
        """ Hide the unit cell of the crystal. """
        self._ngl.remove_unitcell()

    def show_axes(self):
        """ Show the cartesian axis in the corner of the figure. """
        if self._axes is not None:
            return
        self._axes = (
            self._make_arrow(_x_axis),
            self._make_arrow(_y_axis),
            self._make_arrow(_z_axis),
        )

    def hide_axes(self):
        """ Hide the cartesian axis. """
        if self._axes is None:
            return
        for axis in self._axes:
            self._ngl.remove_component(axis)
        self._axes = None

    def show_arrows_at_atoms(self, arrows, color=[0.1, 0.1, 0.8]):
        """ Add arrows at all the atoms.

        Parameters
        ----------
        arrows : np.ndarray
            An array containing the direction of an arrow for every atom in the
            unit cell. This arrow will be drawn in the figure.
        color : np.ndarray
            rgb values of the arrow, defaulting to blue.

        Notes
        -----
        If you are working on a supercell, the code will automatically extend the
        size of the array to show arrows in the supercell, too.
        """
        if self._positions is None:
            raise RefinementException("Positions of atoms are not known.")
        arrows = np.repeat(arrows, self._multiple_cells, axis=0)
        for tail, arrow in zip(self._positions, arrows):
            tip = tail + arrow
            arrow = _Arrow3d(tail, tip, color)
            self._arrows.append(self._make_arrow(arrow))

    def hide_arrows_at_atoms(self):
        """ Remove all arrows from the atoms.

        Notes
        -----
        If two different kind of atoms have been added to the system, there is
        currently no option to distinguish between them."""
        for arrow in self._arrows:
            self._ngl.remove_component(arrow)
        self._arrows = []

    def _make_arrow(self, arrow):
        return self._ngl.shape.add_arrow(*(arrow.to_serializable()))