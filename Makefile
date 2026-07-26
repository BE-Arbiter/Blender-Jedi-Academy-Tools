PY_FILES = __init__.py mod_reload.py casts.py error_types.py JAAseExport.py JAAseImport.py JAFilesystem.py JAG2AnimationCFG.py JAG2Constants.py JAG2GLA.py JAG2GLM.py JAG2Math.py JAG2Operators.py JAG2Panels.py JAG2Scene.py JAMaterialmanager.py JAMd3Encode.py JAMd3Export.py JAPatchExport.py JARoffExport.py JARoffImport.py JAStringhelper.py MrwProfiler.py

ZIP_CONTENTS = $(PY_FILES) jediacademy_plugins_readme.txt

# ASE/ROFF/MD3/Patch: unverified, not actively maintained (see CLAUDE.md) -- excluded from
# format/pep8 the same way pyrightconfig.json already excludes them from typechecking.
PEP8_EXCLUDE = JAAseExport.py JAAseImport.py JAMd3Encode.py JAMd3Export.py JAPatchExport.py JARoffExport.py JARoffImport.py
PEP8_FILES = $(filter-out $(PEP8_EXCLUDE),$(PY_FILES)) tests/*.py

.PHONY: all format pep8

all: jediacademy.zip jediacademy_plugins_doc.pdf

format:
	autopep8 --in-place $(PEP8_FILES)

pep8:
	pycodestyle --config=.pep8 $(PEP8_FILES)

build/jediacademy.zip: $(ZIP_CONTENTS)
# we must first create the desired directory structure for the zip,
# i.e. the top-level "jediacademy" folder
	mkdir -p build/jediacademy
	cp $(ZIP_CONTENTS) build/jediacademy
	(cd build; zip -r jediacademy.zip jediacademy)

build/jediacademy_plugins_doc.pdf: jediacademy_plugins_doc.tex
	mkdir -p build
	pdflatex --output-directory=build jediacademy_plugins_doc.tex
