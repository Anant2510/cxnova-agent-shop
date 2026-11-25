"""The content repository a case is filed to once it is assessed (and can be pulled from).

    local      a folder that stands in for a content platform: <data>/_repository/<tenant>/<reference>/ with an index.json
    omnidocs   NewgenONE OmniDocs, through its REST web services

    DOCUINSIGHTS_REPOSITORY        local (default) | omnidocs
    OMNIDOCS_URL                   https://<server>:<port>            (the services live under /OmniDocsRestWS/rest/services)
    OMNIDOCS_CABINET, OMNIDOCS_USER, OMNIDOCS_PASSWORD
    OMNIDOCS_FOLDER_INDEX          the folder the case documents are added to
    OMNIDOCS_VOLUME_ID             the volume of that folder
    OMNIDOCS_DATACLASS_INDEX, OMNIDOCS_DATACLASS_NAME     optional: the data class to index the documents with
    OMNIDOCS_FIELD_MAP             optional JSON: which data-class field takes which value, for example
                                   {"case_reference": {"indexId": "101", "indexType": "S"}, "document_type": {"indexId": "102", "indexType": "S"}}
                                   values on offer: case_reference, document_type, classification_confidence, recommendation, assessment_id, sha256

The OmniDocs connector is written from the "NewgenONE OmniDocs 11.3 REST Web Services Developer Guide" (addDocumentJSON:
multipart/form-data with a text part NGOAddDocumentBDO and a file part `file`; getDocumentStreamJSON). It has NOT been
run against a cabinet: the guide does not show every request in full, so expect to adjust the envelope on first contact
(`OmniDocsRepository.add_document` and `._bdo` are the two places). Folder creation and search are not used: documents
go into one configured folder and are found again by the document index this connector records.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from . import net
from .casefile import current, doc_type, live_docs, now
from .profiles import Profile


class RepositoryError(RuntimeError):
    pass


def metadata_for(case: dict[str, Any], doc: dict[str, Any], profile: Profile) -> dict[str, str]:
    a = case.get("assessment") or {}
    cls = doc.get("classification") or {}
    return {"case_reference": case["reference"], "document_type": profile.type_name(doc_type(doc)), "classification_confidence": str(cls.get("confidence") or ""),
            "recommendation": (a.get("recommendation") or {}).get("label") or "", "assessment_id": a.get("id") or "", "sha256": current(doc).get("sha256", "")}


class LocalRepository:
    kind = "local"
    name = "Stand-in repository (a folder on this server)"

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def for_tenant(self, tenant: str) -> "LocalRepository":
        """Each tenant files into its own folder, so two tenants with the same case reference never meet."""
        return LocalRepository(self.root / tenant)

    def status(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "connected": True, "detail": str(self.root)}

    def file_case(self, case: dict[str, Any], profile: Profile, read: Any) -> dict[str, Any]:
        folder = self.root / case["reference"]
        folder.mkdir(parents=True, exist_ok=True)
        filed = []
        for d in live_docs(case):
            if d.get("assumed"):
                continue
            ver = current(d)
            name = f"{d['id']}_v{d['current']}_{d['name']}"
            (folder / name).write_bytes(read(d, ver))
            filed.append({"doc": d["id"], "name": d["name"], "version": d["current"], "repository_id": f"{case['reference']}/{name}", "metadata": metadata_for(case, d, profile)})
        a = case.get("assessment") or {}
        index = {"reference": case["reference"], "profile": profile.id, "filed_at": now(), "documents": filed, "recommendation": a.get("recommendation"),
                 "summary": a.get("summary"), "findings": [{k: f[k] for k in ("id", "family", "title", "status", "message")} for f in a.get("findings") or []]}
        (folder / "index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
        return {"repository": self.kind, "folder": str(folder), "documents": filed, "filed_at": index["filed_at"]}

    def fetch_case(self, reference: str) -> list[dict[str, Any]]:
        folder = self.root / reference
        index = folder / "index.json"
        if not index.exists():
            raise RepositoryError(f"the repository holds no case '{reference}'")
        out = []
        for row in json.loads(index.read_text(encoding="utf-8")).get("documents") or []:
            out.append({"name": row["name"], "category": "", "data": (self.root / row["repository_id"]).read_bytes()})
        return out


class OmniDocsRepository:
    kind = "omnidocs"
    name = "NewgenONE OmniDocs"

    def __init__(self, env: dict[str, str]):
        self.url = (env.get("OMNIDOCS_URL") or "").rstrip("/")
        self.cabinet, self.user, self.password = env.get("OMNIDOCS_CABINET") or "", env.get("OMNIDOCS_USER") or "", env.get("OMNIDOCS_PASSWORD") or ""
        self.folder, self.volume = env.get("OMNIDOCS_FOLDER_INDEX") or "", env.get("OMNIDOCS_VOLUME_ID") or "1"
        self.dataclass_index, self.dataclass_name = env.get("OMNIDOCS_DATACLASS_INDEX") or "", env.get("OMNIDOCS_DATACLASS_NAME") or ""
        try:
            self.field_map = json.loads(env.get("OMNIDOCS_FIELD_MAP") or "{}")
        except ValueError as exc:
            raise RepositoryError(f"OMNIDOCS_FIELD_MAP is not JSON: {exc}") from exc
        self.timeout = float(env.get("OMNIDOCS_TIMEOUT_S") or 60)

    @property
    def base(self) -> str:
        return f"{self.url}/OmniDocsRestWS/rest/services"

    def status(self) -> dict[str, Any]:
        lacking = [k for k, v in (("OMNIDOCS_URL", self.url), ("OMNIDOCS_CABINET", self.cabinet), ("OMNIDOCS_USER", self.user), ("OMNIDOCS_PASSWORD", self.password),
                                  ("OMNIDOCS_FOLDER_INDEX", self.folder)) if not v]
        return {"kind": self.kind, "name": self.name, "connected": not lacking, "detail": ("configured for cabinet " + self.cabinet) if not lacking else "not configured: set " + ", ".join(lacking),
                "note": "Connector written from the 11.3 REST developer guide; not yet verified against a cabinet."}

    def _bdo(self, document_name: str, extension: str, meta: dict[str, str]) -> dict[str, Any]:
        bdo: dict[str, Any] = {"cabinetName": self.cabinet, "folderIndex": self.folder, "documentName": document_name, "userDBId": "", "volumeId": self.volume,
                               "createdByAppName": extension, "accessType": "I", "versionFlag": "N", "comment": f"Filed by DocuInsights for case {meta.get('case_reference', '')}",
                               "userName": self.user, "userPassword": self.password, "locale": "en_US", "oAuth": "N"}
        rows = [{"indexId": str(spec.get("indexId")), "indexType": str(spec.get("indexType") or "S"), "indexValue": meta[key]}
                for key, spec in self.field_map.items() if key in meta and isinstance(spec, dict) and spec.get("indexId")]
        if self.dataclass_index and rows:
            bdo["NGOAddDocDataDefCriterionBDO"] = {"dataDefIndex": self.dataclass_index, "dataDefName": self.dataclass_name, "NGOAddDocDataDefCriteriaDataBDO": rows}
        return {"NGOAddDocumentBDO": bdo}

    def add_document(self, name: str, data: bytes, meta: dict[str, str]) -> str:
        ext = Path(name).suffix.lstrip(".").lower() or "bin"
        boundary = "----docuinsights" + uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="NGOAddDocumentBDO"\r\nContent-Type: application/json\r\n\r\n'.encode()
                 + json.dumps(self._bdo(Path(name).stem, ext, meta)).encode() + b"\r\n",
                 f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n",
                 f"--{boundary}--\r\n".encode()]
        req = urllib.request.Request(f"{self.base}/addDocumentJSON", data=b"".join(parts), method="POST",
                                     headers={"content-type": f"multipart/form-data; boundary={boundary}", "accept": "application/json"})
        try:
            with net.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RepositoryError(f"OmniDocs answered {exc.code}: {exc.read().decode('utf-8', errors='replace')[:200]}") from exc
        except Exception as exc:
            raise RepositoryError(f"OmniDocs not reachable: {type(exc).__name__}: {exc}") from exc
        out = body.get("NGOAddDocumentResponseBDO") or body
        if str(out.get("status", out.get("statusCode", ""))) != "0":
            raise RepositoryError(f"OmniDocs refused the document: {out.get('message') or out}")
        return str((out.get("NGOGetDocListDocDataBDO") or {}).get("documentIndex") or "")

    def get_document(self, doc_index: str) -> bytes:
        body = json.dumps({"NGOGetDocumentBDO": {"cabinetName": self.cabinet, "docIndex": str(doc_index), "userName": self.user, "userPassword": self.password,
                                                 "userDBId": "", "locale": "en_US", "oAuth": "N"}}).encode()
        req = urllib.request.Request(f"{self.base}/getDocumentStreamJSON", data=body, method="POST", headers={"content-type": "application/json"})
        try:
            with net.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except Exception as exc:
            raise RepositoryError(f"OmniDocs could not return document {doc_index}: {type(exc).__name__}: {exc}") from exc

    def file_case(self, case: dict[str, Any], profile: Profile, read: Any) -> dict[str, Any]:
        st = self.status()
        if not st["connected"]:
            raise RepositoryError(st["detail"])
        filed = []
        for d in live_docs(case):
            if d.get("assumed"):
                continue
            meta = metadata_for(case, d, profile)
            index = self.add_document(f"{case['reference']}_{d['name']}", read(d, current(d)), meta)
            filed.append({"doc": d["id"], "name": d["name"], "version": d["current"], "repository_id": index, "metadata": meta})
        return {"repository": self.kind, "folder": self.folder, "cabinet": self.cabinet, "documents": filed, "filed_at": now()}

    def fetch_case(self, reference: str) -> list[dict[str, Any]]:
        raise RepositoryError("pulling a case package by reference needs the search call of your OmniDocs build; pass document indexes to get_document() instead")


def from_env(data_dir: str | Path, env: dict[str, str] | None = None) -> Any:
    env = env if env is not None else dict(os.environ)
    kind = (env.get("DOCUINSIGHTS_REPOSITORY") or "local").strip().lower()
    if kind == "omnidocs":
        return OmniDocsRepository(env)
    if kind != "local":
        raise RepositoryError(f"unknown DOCUINSIGHTS_REPOSITORY '{kind}' (local, omnidocs)")
    return LocalRepository(Path(data_dir) / "_repository")   # '_' keeps it out of the tenant names (a tenant starts with a letter or a digit)
