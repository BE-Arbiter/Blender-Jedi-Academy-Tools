"""Regenerates tests/testdata/simpleskel_nla.blend: imports simpleskel.gla via the
animation.cfg/NLA path (AnimationLoadMode.CFG) so the fixture captures the NLA-track state that
import produces, for case_nla_export/case_nla_roundtrip in tests/run_tests.py to exercise.

Run via the blender-run-script skill, pinned to Blender 4.1 (the add-on's minimum supported
version) so the resulting .blend stays openable by every supported version:
  .claude/skills/blender-run-script/run_blender_script.sh 4.1 tests/tools/generate_simpleskel_nla_blend.py
"""
import os
import sys

import bpy

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import testutil  # noqa: E402 - path must be set up first

addon = testutil.import_addon()
addon.register()
testutil.reset_scene()  # clear Blender's default startup scene (Cube/Camera/Light)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASEPATH = os.path.join(REPO_ROOT, "tests", "testdata", "GameData", "base")
SKELETON_REL = "models/testcases/simpleskel/simpleskel"

scene = addon.JAG2Scene.Scene(BASEPATH)

cfg_dir = addon.JAFilesystem.PathToFile(SKELETON_REL, BASEPATH)
success, message = scene.loadFromCFG(cfg_dir)
assert success, message

success, message = scene.loadFromGLA(SKELETON_REL, loadAnimations=addon.JAG2GLA.AnimationLoadMode.CFG)
assert success, message

success, message = scene.saveToBlender(
    scale=1.0, skin_rel="", guessTextures=False, useAnimation=True,
    skeletonFixes=addon.JAG2Constants.SkeletonFixes.NONE,
)
assert success, message

out_path = os.path.join(REPO_ROOT, "tests", "testdata", "simpleskel_nla.blend")
bpy.ops.wm.save_as_mainfile(filepath=out_path)
print(f"Saved {out_path}")
