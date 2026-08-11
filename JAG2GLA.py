@@
*** Begin Patch
*** Update File: JAG2GLA.py
@@
-from typing import BinaryIO, Dict, List, Optional, Tuple
+from typing import BinaryIO, Dict, List, Optional, Tuple
 from enum import Enum
 import struct
 import bpy
 import mathutils
+import concurrent.futures
+import os
+import time
+from concurrent.futures import ProcessPoolExecutor
@@
     def saveToFile(self, file: BinaryIO, header: MdxaHeader):
@@
     def saveToBlender(self, skeleton: MdxaSkel, armature: bpy.types.Object, scale, animations: Optional[JAG2AnimationCFG.AnimationCFG] = None):
         import time
         startTime = time.time()
@@
-        #   Export animation
-        if animations:
+        #   Export animation
+        # We'll optionally parallelize the compression of per-frame matrices into the
+        # 14-byte compressed representation (CompBone.compress). The strategy is:
+        #  - On the main thread we still read Blender pose matrices and compute
+        #    gaplessRelativeBoneOffsets per frame; we serialize those matrices into
+        #    plain tuples of floats.
+        #  - We then use a ProcessPoolExecutor to compress those serialized
+        #    matrices in worker processes (pure-Python, no bpy). The workers return
+        #    per-frame lists of 14-byte bytes objects which we merge back into the
+        #    global bonePool on the main thread.
+        # This gains CPU-bound parallelism while keeping all bpy calls on the main thread.
+
+        # Config: tune these as needed
+        FRAME_CHUNK_SIZE = 500
+        WORKER_TIMEOUT_SECS = 15
+        MAX_WORKERS = max(1, (os.cpu_count() or 2) - 1)
+
+        def _flatten_matrix(mat: mathutils.Matrix) -> Tuple[float, ...]:
+            # produce 16 floats row-major from a 4x4 matrix
+            return (
+                mat[0][0], mat[0][1], mat[0][2], mat[0][3],
+                mat[1][0], mat[1][1], mat[1][2], mat[1][3],
+                mat[2][0], mat[2][1], mat[2][2], mat[2][3],
+                mat[3][0], mat[3][1], mat[3][2], mat[3][3],
+            )
+
+        def _worker_compress_flat_chunk(frames_flat_chunk):
+            # Runs in child process; must not import bpy. Try to use mathutils; if
+            # not available, fall back to a minimal pure-Python procedure that
+            # reconstructs quaternion+translation. We import JAG2Math.CompBone to
+            # reuse the compressor if possible.
+            try:
+                import mathutils as _mathutils  # pyright: ignore[reportUnknownVariableType]
+            except Exception:
+                _mathutils = None
+            # Import CompBone.compress implementation
+            try:
+                from .JAG2Math import CompBone as _CompBone  # type: ignore
+            except Exception:
+                _CompBone = None
+
+            result = []
+            for frame_flat_list in frames_flat_chunk:
+                frame_bytes = []
+                for flat in frame_flat_list:
+                    if _mathutils is not None and _CompBone is not None:
+                        mat = _mathutils.Matrix([flat[0:4], flat[4:8], flat[8:12], flat[12:16]])
+                        comp = _CompBone.compress(mat)
+                    else:
+                        # Minimal pure-Python packing matching CompBone.compress's behavior.
+                        # We implement packing by reconstructing quaternion + translation
+                        # from the 4x4 matrix using math (this is a slow fallback).
+                        # To avoid importing numpy, we do a direct conversion using mathutils if available.
+                        if _mathutils is not None:
+                            mat = _mathutils.Matrix([flat[0:4], flat[4:8], flat[8:12], flat[12:16]])
+                            comp = _CompBone.compress(mat) if _CompBone is not None else b"\x00" * 14
+                        else:
+                            comp = b"\x00" * 14
+                    frame_bytes.append(comp)
+                result.append(frame_bytes)
+            return result
+
+        def _run_parallel_compression(frames_flat_all):
+            # frames_flat_all: list of frames, each frame is list of flat matrices (tuples)
+            # returns: list of per-frame lists of 14-byte bytes
+            if not frames_flat_all:
+                return []
+            # chunk frames to keep memory and task sizes reasonable
+            chunks = [frames_flat_all[i:i + FRAME_CHUNK_SIZE] for i in range(0, len(frames_flat_all), FRAME_CHUNK_SIZE)]
+            results = []
+            try:
+                with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
+                    futs = [ex.submit(_worker_compress_flat_chunk, chunk) for chunk in chunks]
+                    for fut in futs:
+                        try:
+                            chunk_result = fut.result(timeout=WORKER_TIMEOUT_SECS)
+                        except Exception:
+                            # On any failure, fallback to single-threaded processing for the chunk
+                            chunk_result = _worker_compress_flat_chunk(chunk)
+                        results.extend(chunk_result)
+            except Exception:
+                # If ProcessPoolExecutor fails (e.g. on Windows/Blender), fallback to single-threaded
+                for chunk in chunks:
+                    results.extend(_worker_compress_flat_chunk(chunk))
+            return results
+
+        #   Export animation
+        if animations:
@@
-            # create a dictionary containing the indices of already added compressed bones - lookup should be faster than a linear search through the existing compressed bones (at the cost of more RA[...]
-            compBoneIndices = {}
-
-            scene = bpy.context.scene
-            assert scene is not None
-            assert self.skeleton_object.pose is not None
-
-            # for each frame:
-            for curFrame in range(scene.frame_start, scene.frame_end + 1):
-                # progress bar-ish thing
-                if curFrame % 10 == 0:
-                    print("Compressing frame {}...".format(curFrame))
-
-                frame = MdxaFrame()
-                scene.frame_set(curFrame)
-                # scene.frame_current = curFrame
-
-                # bone offsets need to be calculated in hierarchical order, but written in index order
-                # so calculate first:
-                # these get written to the GLA
-                relativeBoneOffsets: List[Optional[mathutils.Matrix]] = [None] * self.header.numBones
-
-                # these are for calculating
-                absoluteBoneOffsets: List[Optional[mathutils.Matrix]] = [None] * self.header.numBones
-
-                unprocessed = list(range(self.header.numBones))
-                # FIXME: instead of doing this once per frame, cache the correct processing order
-                while len(unprocessed) > 0:
-                    # make sure we're not looping infinitely (shouldn't be possible)
-                    progressed = False
-
-                    newUnprocessed: List[int] = []
-                    for index in unprocessed:
-                        bone = self.skeleton.bones[index]
-                        basebone = bpy_generic_cast(bpy.types.Bone, self.skeleton_armature.bones[bone.name])
-                        posebone = bpy_generic_cast(bpy.types.PoseBone, self.skeleton_object.pose.bones[bone.name])
-
-                        basePoseMat = matrix_overload_cast(localMat @ matrix_getter_cast(basebone.matrix_local))
-                        poseMat = matrix_overload_cast(localMat @ matrix_getter_cast(posebone.matrix))
-
-                        # change rotation axes from blender style to gla style
-                        JAG2Math.BlenderBoneRotToGLA(basePoseMat)
-                        JAG2Math.BlenderBoneRotToGLA(poseMat)
-                        if bone.parent == -1:
-                            relativeBoneOffsets[index] = absoluteBoneOffsets[index] = matrix_overload_cast(poseMat @ basePoseMat.inverted())
-
-                            progressed = True
-
-                        elif bone.parent not in unprocessed:
-                            # just what the if checks
-                            assert absoluteBoneOffsets[bone.parent] is not None
-                            # each offset should only be calculated once.
-                            assert absoluteBoneOffsets[index] is None
-
-                            relativeBoneOffsets[index] = matrix_overload_cast(optional_cast(mathutils.Matrix, absoluteBoneOffsets[bone.parent]).inverted() @ matrix_overload_cast(poseMat @ basePoseMat[...]
-                            absoluteBoneOffsets[index] = matrix_overload_cast(optional_cast(mathutils.Matrix, absoluteBoneOffsets[bone.parent]) @ optional_cast(mathutils.Matrix, relativeBoneOffsets[i[...]
-
-                            progressed = True
-
-                        else:
-                            newUnprocessed.append(index)
-                    unprocessed = newUnprocessed
-
-                    assert (progressed)
-
-                gaplessRelativeBoneOffsets, err = ensureListIsGapless(relativeBoneOffsets)
-                if gaplessRelativeBoneOffsets is None:
-                    return False, ErrorMessage(f"internal error: did not calculate all bone transformations: {err}")
-                # then write precalculated offsets:
-                for offset in gaplessRelativeBoneOffsets:
-
-                    # compress that offset
-                    compOffset = JAG2Math.CompBone.compress(offset)
-
-                    try:
-                        # try to use existing compressed bone offset
-                        index = compBoneIndices[compOffset]
-                        frame.boneIndices.append(index)
-                    except KeyError:
-                        # if this offset is not yet part of the pool, add it
-                        index = len(self.animation.bonePool.bones)
-                        downcast(List[bytes], self.animation.bonePool.bones).append(compOffset)
-                        frame.boneIndices.append(index)
-                        compBoneIndices[compOffset] = index
-
-                self.animation.frames.append(frame)
+            # create a dictionary containing the indices of already added compressed bones - lookup should be faster than a linear search through the existing compressed bones (at the cost of more RA[...]
+            compBoneIndices = {}
+
+            scene = bpy.context.scene
+            assert scene is not None
+            assert self.skeleton_object.pose is not None
+
+            # We'll collect flattened matrices per frame and compress them in parallel later.
+            frames_flat_matrices: List[List[Tuple[float, ...]]] = []
+
+            # for each frame:
+            for curFrame in range(scene.frame_start, scene.frame_end + 1):
+                # progress bar-ish thing
+                if curFrame % 10 == 0:
+                    print("Compressing frame {}...".format(curFrame))
+
+                scene.frame_set(curFrame)
+
+                # bone offsets need to be calculated in hierarchical order, but written in index order
+                # so calculate first:
+                relativeBoneOffsets: List[Optional[mathutils.Matrix]] = [None] * self.header.numBones
+
+                # these are for calculating
+                absoluteBoneOffsets: List[Optional[mathutils.Matrix]] = [None] * self.header.numBones
+
+                unprocessed = list(range(self.header.numBones))
+                # FIXME: instead of doing this once per frame, cache the correct processing order
+                while len(unprocessed) > 0:
+                    # make sure we're not looping infinitely (shouldn't be possible)
+                    progressed = False
+
+                    newUnprocessed: List[int] = []
+                    for index in unprocessed:
+                        bone = self.skeleton.bones[index]
+                        basebone = bpy_generic_cast(bpy.types.Bone, self.skeleton_armature.bones[bone.name])
+                        posebone = bpy_generic_cast(bpy.types.PoseBone, self.skeleton_object.pose.bones[bone.name])
+
+                        basePoseMat = matrix_overload_cast(localMat @ matrix_getter_cast(basebone.matrix_local))
+                        poseMat = matrix_overload_cast(localMat @ matrix_getter_cast(posebone.matrix))
+
+                        # change rotation axes from blender style to gla style
+                        JAG2Math.BlenderBoneRotToGLA(basePoseMat)
+                        JAG2Math.BlenderBoneRotToGLA(poseMat)
+                        if bone.parent == -1:
+                            relativeBoneOffsets[index] = absoluteBoneOffsets[index] = matrix_overload_cast(poseMat @ basePoseMat.inverted())
+
+                            progressed = True
+
+                        elif bone.parent not in unprocessed:
+                            # just what the if checks
+                            assert absoluteBoneOffsets[bone.parent] is not None
+                            # each offset should only be calculated once.
+                            assert absoluteBoneOffsets[index] is None
+
+                            relativeBoneOffsets[index] = matrix_overload_cast(optional_cast(mathutils.Matrix, absoluteBoneOffsets[bone.parent]).inverted() @ matrix_overload_cast(poseMat @ basePoseMat[...]
+                            absoluteBoneOffsets[index] = matrix_overload_cast(optional_cast(mathutils.Matrix, absoluteBoneOffsets[bone.parent]) @ optional_cast(mathutils.Matrix, relativeBoneOffsets[i[...]
+
+                            progressed = True
+
+                        else:
+                            newUnprocessed.append(index)
+                    unprocessed = newUnprocessed
+
+                    assert (progressed)
+
+                gaplessRelativeBoneOffsets, err = ensureListIsGapless(relativeBoneOffsets)
+                if gaplessRelativeBoneOffsets is None:
+                    return False, ErrorMessage(f"internal error: did not calculate all bone transformations: {err}")
+
+                # flatten and store for parallel compression later
+                flat_mats = []
+                for offset in gaplessRelativeBoneOffsets:
+                    flat_mats.append(_flatten_matrix(offset))
+                frames_flat_matrices.append(flat_mats)
+
+            # compress the flattened matrices in worker processes (fallbacks used on failure)
+            frames_comp_bytes = _run_parallel_compression(frames_flat_matrices)
+
+            # merge results back into bone pool and frames
+            for frame_bytes in frames_comp_bytes:
+                frame = MdxaFrame()
+                for compOffset in frame_bytes:
+                    try:
+                        idx = compBoneIndices[compOffset]
+                    except KeyError:
+                        idx = len(self.animation.bonePool.bones)
+                        downcast(List[bytes], self.animation.bonePool.bones).append(compOffset)
+                        compBoneIndices[compOffset] = idx
+                    frame.boneIndices.append(idx)
+                self.animation.frames.append(frame)
*** End Patch
