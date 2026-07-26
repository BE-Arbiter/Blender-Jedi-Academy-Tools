---
name: blender-run-script
description: Run an arbitrary one-off Python script headlessly inside a pinned Blender version via podman. Use for anything the fixed test suite (blender-tests) doesn't cover -- generating/regenerating a .blend fixture, inspecting .gla/.glm data that needs bpy/mathutils, or any other ad-hoc headless Blender task.
---

# Run an arbitrary script in headless Blender

A generic counterpart to `.claude/skills/blender-tests`, which only runs the fixed
`tests/run_tests.py` suite. This one takes any script path instead, for the recurring need to run
one-off headless Blender work (e.g. generating a `.blend` test fixture) without having to craft a
fresh `podman run ... -v "$(pwd)":/repo:Z ...` invocation each time -- the `$(pwd)`-based mount is
what makes the ad-hoc version unsafe to whitelist as a fixed pattern.

```
.claude/skills/blender-run-script/run_blender_script.sh <version> <script-path-relative-to-repo> [-- <args> ...]
# e.g.
.claude/skills/blender-run-script/run_blender_script.sh 4.1 tests/tools/generate_simpleskel_nla_blend.py
```

Pulls `docker.io/blenderkit/headless-blender:blender-<version>-stable` if not already present,
mounts the repo read-write at `/repo` inside the container, and runs
`blender --background --python-exit-code 1 --python /repo/<script-path> -- <args>`. Doesn't rely on
the caller's `$(pwd)` or take a repo path -- it derives the repo root from its own on-disk
location, so call it directly from any cwd.

Pin the version deliberately: a `.blend` saved by a newer Blender can't be opened by an older one
(see the add-on's minimum supported version in `bl_info["blender"]`, `__init__.py`), so fixtures
meant to stay openable on the oldest supported version must be generated with that version, not
whatever's newest.
