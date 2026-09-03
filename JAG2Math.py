# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

from .mod_reload import reload_modules
reload_modules(locals(), __package__, ["JAG2Constants"], [])  # nopep8

from . import JAG2Constants

import struct
from typing import BinaryIO, Optional, Tuple
import mathutils

# 3 * 4 : shear not used.


class Matrix:
    def __init__(self):
        self.rows = []
        for i in range(3):
            self.rows.append([0, 0, 0, 0])
        self.rows[0][0] = 1
        self.rows[1][1] = 1
        self.rows[2][2] = 1

    def loadFromFile(self, file: BinaryIO) -> None:
        for y in range(3):
            for x in range(4):
                self.rows[y][x], = struct.unpack("<f", file.read(4))

    def saveToFile(self, file: BinaryIO) -> None:
        for y in range(3):
            for x in range(4):
                file.write(struct.pack("<f", self.rows[y][x]))

    def toBlender(self) -> mathutils.Matrix:
        mat = mathutils.Matrix(
            [self.rows[0], self.rows[1], self.rows[2], [0, 0, 0, 1]])
        return mat

    def fromBlender(self, mat: mathutils.Matrix) -> None:
        mat = mathutils.Matrix(mat)  # pyright: ignore [reportArgumentType]  # matrix supports slices
        mat.to_4x4()
        self.rows = []
        for row in mat:
            rowList = []
            rowList.extend(row)  # pyright: ignore [reportArgumentType]  # vector is iterable
            self.rows.append(rowList)
        del self.rows[3]


def GLABoneRotToBlender(matrix: mathutils.Matrix) -> None:
    """
    Changes a GLA bone's rotation matrix (X+ = front) to blender style (Y+ = front)
    """
    new_x = -matrix.col[1].copy()
    new_y = matrix.col[0].copy()
    matrix.col[0] = new_x
    matrix.col[1] = new_y
    # undo change in translation
    matrix[3][0], matrix[3][1] = matrix[3][1], -matrix[3][0]

    # also, roll 90 degrees
    matrix[0][0], matrix[1][0], matrix[2][0], matrix[0][2], matrix[1][2], matrix[2][2] = - \
        matrix[0][2], -matrix[1][2], - \
        matrix[2][2], matrix[0][0], matrix[1][0], matrix[2][0]


def BlenderBoneRotToGLA(matrix: mathutils.Matrix) -> None:
    """
    Changes a blender bone's rotation matrix (Y+ = front) to GLA style (X+ = front)
    """
    # undo roll 90 degrees
    matrix[0][0], matrix[1][0], matrix[2][0], matrix[0][2], matrix[1][2], matrix[2][2] = matrix[0][2], matrix[1][2], matrix[2][2], - \
        matrix[0][0], -matrix[1][0], -matrix[2][0]

    new_x = matrix.col[1].copy()
    new_y = -matrix.col[0].copy()
    matrix.col[0] = new_x
    matrix.col[1] = new_y
    # undo change in translation
    matrix[3][0], matrix[3][1] = matrix[3][1], -matrix[3][0]


# Quantisation constants of the format. The engine decodes with exactly these, so they
# must not be changed. Kept here rather than inline so the load and save paths provably
# use the same numbers.
COMPBONE_QUAT_SCALE = 16383.0
COMPBONE_QUAT_BIAS = 2.0
COMPBONE_QUAT_MAX = 2.0
COMPBONE_XLAT_BIAS = 512.0
# (511 + 512) * 64 = 65472 still fits a uint16; 512 would give 65536 and overflow.
COMPBONE_XLAT_MAX = 511.0


class CompressionStats:
    """Counts values that had to be clamped to stay representable.

    Without this, an out-of-range bone made struct.pack raise a bare struct.error in the
    middle of the export, with a traceback that named neither the bone nor the frame."""

    def __init__(self):
        self.quatClamped = 0
        self.xlatClamped = 0
        self.maxXlatSeen = 0.0
        self.worst = ""

    def clean(self) -> bool:
        return self.quatClamped == 0 and self.xlatClamped == 0

    def summary(self) -> str:
        if self.clean():
            return "nothing out of range"
        parts = []
        if self.quatClamped:
            parts.append("{} quaternion component(s) clamped".format(self.quatClamped))
        if self.xlatClamped:
            parts.append("{} translation(s) clamped (max |t| = {:.2f} at {})".format(
                self.xlatClamped, self.maxXlatSeen, self.worst or "?"))
        return "; ".join(parts)


def _squash(value: float, bias: float, scale: float, limit: float) -> Tuple[int, bool]:
    """Quantise to uint16, returning (raw, wasClamped).

    The range test also catches NaN, since every comparison against NaN is False - a NaN
    would otherwise reach struct.pack via int(round(nan)) and raise there instead."""
    clamped = False
    if not (-limit <= value <= limit):
        clamped = True
        value = 0.0 if value != value else max(-limit, min(limit, value))
    raw = int(round((value + bias) * scale))
    # Belt and braces: rounding at the very edge of the range must not step outside it.
    if raw < 0:
        raw = 0
    elif raw > 65535:
        raw = 65535
    return raw, clamped


# compressed bones as used in GLA files


class CompBone:
    def __init__(self, matrix: mathutils.Matrix):
        self.matrix = matrix

    @staticmethod
    def loadFromFile(file: BinaryIO) -> "CompBone":
        quat = mathutils.Quaternion()
        loc = mathutils.Vector([0, 0, 0, 1])  # make sure it's 4 dimensional
        # 14 bytes: 4 shorts for quat = 8 bytes, 3 shorts for position = 6 bytes
        q_w, q_x, q_y, q_z, l_x, l_y, l_z = struct.unpack("<7H", file.read(14))
        # map quaternion values from 0..65535 to -2..2
        quat.w = (q_w / COMPBONE_QUAT_SCALE) - COMPBONE_QUAT_BIAS
        quat.x = (q_x / COMPBONE_QUAT_SCALE) - COMPBONE_QUAT_BIAS
        quat.y = (q_y / COMPBONE_QUAT_SCALE) - COMPBONE_QUAT_BIAS
        quat.z = (q_z / COMPBONE_QUAT_SCALE) - COMPBONE_QUAT_BIAS
        # These encode a rotation, so the quaternion is meant to be unit - but 14 bit
        # quantisation leaves it off by up to ~0.0015, and Quaternion.to_matrix() does not
        # normalise. The resulting matrix therefore carries a small scale, and since absolute
        # transforms are built by multiplying offsets down the hierarchy, that scale compounds:
        # measured on _humanoid-ja.gla it reaches 8.9% at the deepest bones, displacing them by
        # up to 0.15 units. Normalising here keeps every offset a pure rotation.
        if quat.magnitude > 1e-9:
            quat.normalize()
        else:
            quat.identity()
        # map location from 0..65535 to -512..512 (511.984375)
        loc.x = (l_x / JAG2Constants.COMPBONE_LOCATION_STEPS_PER_UNIT) - COMPBONE_XLAT_BIAS
        loc.y = (l_y / JAG2Constants.COMPBONE_LOCATION_STEPS_PER_UNIT) - COMPBONE_XLAT_BIAS
        loc.z = (l_z / JAG2Constants.COMPBONE_LOCATION_STEPS_PER_UNIT) - COMPBONE_XLAT_BIAS

        # turn rotation into matrix
        matrix = quat.to_matrix()
        # resize to 4x4 so we can add translation
        matrix.resize_4x4()
        # add translation
        matrix.col[3] = loc
        assert (matrix[3][3] == 1)
        # convert to blender style
        # shouldn't be done until all offsets have been combined.
        # GLABoneRotToBlender(self.matrix)

        return CompBone(matrix)

    # returns the 14 byte compressed representation of this matrix (no scale) as saved in the compBonePool
    # For values inside the representable range the output is byte-identical to the previous
    # unclamped version - round() is unchanged, and "<" is a no-op on little-endian hosts.
    # Out-of-range values used to raise struct.error mid-export; now they clamp and get counted,
    # so loadFromBlender can fail with a message naming the bone and frame instead.
    @staticmethod
    def compress(mat: mathutils.Matrix,
                 stats: Optional[CompressionStats] = None,
                 boneName: str = "",
                 frameNum: int = -1) -> bytes:
        loc = mat.to_translation()
        quat = mat.to_quaternion()
        raw = []

        for component in (quat.w, quat.x, quat.y, quat.z):
            value, clamped = _squash(
                component, COMPBONE_QUAT_BIAS, COMPBONE_QUAT_SCALE, COMPBONE_QUAT_MAX)
            if clamped and stats is not None:
                stats.quatClamped += 1
                if not stats.worst:
                    stats.worst = "{} @ frame {}".format(boneName, frameNum)
            raw.append(value)

        for component in (loc.x, loc.y, loc.z):
            value, clamped = _squash(
                component, COMPBONE_XLAT_BIAS,
                JAG2Constants.COMPBONE_LOCATION_STEPS_PER_UNIT, COMPBONE_XLAT_MAX)
            if clamped and stats is not None:
                stats.xlatClamped += 1
                magnitude = 0.0 if component != component else abs(component)
                if magnitude > stats.maxXlatSeen:
                    stats.maxXlatSeen = magnitude
                    stats.worst = "{} @ frame {}".format(boneName, frameNum)
            raw.append(value)

        return struct.pack("<7H", *raw)
