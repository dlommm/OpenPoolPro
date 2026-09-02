"""On-disk layout for contract PDFs (service layer, no router imports).

Each contract's PDFs live under DATA_DIR/contracts/{id}/ with fixed filenames
(contract.pdf, signed.pdf): no user-controlled filename, so no path-traversal
surface on the serve routes. Both the contracts router and the public sign
router need these paths — they live here so routers never import each other.
"""
import os

from app import config


def contract_dir(cid: int) -> str:
    d = os.path.join(config.DATA_DIR, "contracts", str(cid))
    os.makedirs(d, exist_ok=True)
    return d


def pdf_path(contract) -> str:
    return os.path.join(contract_dir(contract.id), "contract.pdf")


def signed_path(contract) -> str:
    return os.path.join(contract_dir(contract.id), "signed.pdf")
