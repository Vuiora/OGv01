"""Locate the preserved source layouts from one checkout."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = {
    "sdl": "F-20260919-StatisticalDiscoveryLearning",
    "mtbmt": "MTBMT/src",
    "plda": "F-20260914-Utils/ParallelLogicDeterminationAlgorithModule",
    "clc": "F-20260914-Utils/ConceptLayerConstructor",
    "crd": "F-20260914-Utils/ConceptRelationDraw",
    "tpwf": "F-20260914-Utils/ThePicWorkingFlow",
    "mentor": "F-20260914-Utils/Mentor/src",
    "renew": "F-260908-GPTSelf-renew",
}

def activate():
    for relative in PROJECTS.values():
        path = ROOT / relative
        if not path.is_dir():
            raise RuntimeError(f"Subproject source missing: {relative}")
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

