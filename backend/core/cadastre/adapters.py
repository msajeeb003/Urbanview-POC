"""Source adapters of the cadastral base, behind one interface and selected by configuration.

- ``uzn_geoportal`` and ``emapa`` (parcels): an export file the source delivered, or a WFS layer
  the source offers, snapshotted to a local GeoPackage first;
- ``ekatastar`` (ownership and legal-burden flags): an attribute export delivered under an
  agreement. The eKatastar web application is never queried: it is a per-parcel lookup, and its
  records name owners.

Every adapter first checks that bulk access is recorded as confirmed in the profile (with the
basis of the access and the licence) and otherwise stops with a clear message: nothing here
scrapes a web page, and nothing falls back to scraping. Every acquisition is recorded with the
source, the retrieval date, the licence note and the file's SHA-256.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.cadastre.config import CadastreProfile, SourceConfig
from core.cadastre.ogr import Ogr


class AccessNotConfirmed(RuntimeError):
    """Bulk access to the source is not confirmed: the import stops here."""


class SourceError(RuntimeError):
    """The source cannot provide what was asked (a method it does not offer, a missing file)."""


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """Provenance of one acquired export (stored on the dataset row)."""

    source_id: str
    source_name: str
    source_url: str | None
    method: str
    retrieved_at: datetime
    access_basis: str | None
    licence_note: str | None
    file_name: str
    file_sha256: str
    file_size: int
    path: Path  # the local file the import reads

    def as_json(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "method": self.method,
            "retrieved_at": self.retrieved_at.isoformat(),
            "access_basis": self.access_basis,
            "licence_note": self.licence_note,
            "file_name": self.file_name,
            "file_sha256": self.file_sha256,
            "file_size": self.file_size,
        }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refusal(config: SourceConfig, municipality_id: str) -> str:
    where = f" ({config.url})" if config.url else ""
    return (
        f"Bulk access to {config.name}{where} is not confirmed, so nothing is imported from it. "
        "The loader reads only an export or a service the source has agreed to provide "
        "(P0: availability and licensing to verify). Once it is confirmed, record it in "
        f"[cadastre.sources.{config.id}] of municipalities/{municipality_id}.toml: "
        'access = "confirmed", access_basis (the agreement or written permission) and '
        "licence_note. There is no scraping fallback."
    )


class CadastreAdapter(ABC):
    """One source of the cadastral base."""

    def __init__(self, config: SourceConfig, municipality_id: str) -> None:
        self.config = config
        self.municipality_id = municipality_id

    @property
    def id(self) -> str:
        return self.config.id

    def check_access(self) -> None:
        if self.config.access != "confirmed":
            raise AccessNotConfirmed(refusal(self.config, self.municipality_id))

    def _file_record(self, path: Path, method: str, retrieved_at: datetime | None) -> SourceRecord:
        if not path.is_file():
            raise SourceError(f"{path}: no such export file")
        return SourceRecord(
            source_id=self.config.id,
            source_name=self.config.name,
            source_url=self.config.url,
            method=method,
            retrieved_at=retrieved_at or datetime.now(UTC),
            access_basis=self.config.access_basis,
            licence_note=self.config.licence_note,
            file_name=path.name,
            file_sha256=sha256_of(path),
            file_size=path.stat().st_size,
            path=path,
        )

    @abstractmethod
    def acquire(
        self,
        workdir: Path,
        *,
        path: Path | None = None,
        retrieved_at: datetime | None = None,
        ogr: Ogr | None = None,
    ) -> SourceRecord:
        """The export as a local file with its provenance (after ``check_access``)."""


class OgrParcelSource(CadastreAdapter):
    """Parcels from an export file or a WFS layer (UZN geoportal, eMapa)."""

    def acquire(
        self,
        workdir: Path,
        *,
        path: Path | None = None,
        retrieved_at: datetime | None = None,
        ogr: Ogr | None = None,
    ) -> SourceRecord:
        self.check_access()
        cfg = self.config
        if path is not None:
            if "file" not in cfg.methods:
                raise SourceError(f"{cfg.name} does not deliver files (methods: {cfg.methods})")
            return self._file_record(path, "file", retrieved_at)
        if cfg.method != "wfs":
            raise SourceError(f"{cfg.name}: give the export file (--file); method is {cfg.method}")
        assert cfg.wfs_url and cfg.wfs_typename  # the config validator guarantees them
        now = datetime.now(UTC)
        snapshot = workdir / f"{cfg.id}-wfs-{now:%Y%m%dT%H%M%SZ}.gpkg"
        typenames = [cfg.wfs_typename] + ([cfg.wfs_ko_typename] if cfg.wfs_ko_typename else [])
        (ogr or Ogr()).snapshot_wfs(cfg.wfs_url, typenames, snapshot, page_size=cfg.wfs_page_size)
        return self._file_record(snapshot, "wfs", now)


class EkatastarOwnershipSource(CadastreAdapter):
    """Ownership and legal-burden flags from an attribute export delivered under an agreement."""

    def acquire(
        self,
        workdir: Path,
        *,
        path: Path | None = None,
        retrieved_at: datetime | None = None,
        ogr: Ogr | None = None,
    ) -> SourceRecord:
        self.check_access()
        if path is None:
            raise SourceError(
                f"{self.config.name} has no bulk service the loader may call: give the attribute "
                "export the source delivered (--ownership FILE)"
            )
        return self._file_record(path, "file", retrieved_at)


ADAPTERS: dict[str, type[CadastreAdapter]] = {
    "uzn_geoportal": OgrParcelSource,
    "emapa": OgrParcelSource,
    "ekatastar": EkatastarOwnershipSource,
}


def adapter_for(profile: CadastreProfile, source_id: str, municipality_id: str) -> CadastreAdapter:
    config = profile.sources.get(source_id)
    if config is None:
        raise SourceError(f"no source {source_id!r} in [cadastre.sources]")
    kind = ADAPTERS.get(source_id)
    if kind is None:
        # a new municipality's source reuses an implementation by kind
        kind = OgrParcelSource if config.kind == "parcels" else EkatastarOwnershipSource
    return kind(config, municipality_id)
