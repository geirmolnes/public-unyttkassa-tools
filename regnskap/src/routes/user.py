"""Per-user lists: favourites, collections and recently viewed companies."""

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src import db
from src.auth import get_user_email
from src.brreg import BrregClient, BrregUnavailable
from src.parsing import orgnr_error

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

ROW_CONCURRENCY = 4
MAX_COLLECTION_SIZE = 200


def get_client(request: Request) -> BrregClient:
    return request.app.state.brreg


def _valid_orgnr(orgnr: str) -> str:
    if error := orgnr_error(orgnr):
        raise HTTPException(400, error)
    return orgnr


async def _name_of(client: BrregClient, orgnr: str) -> str:
    """Company name from Brreg; 404 if the orgnr doesn't exist, 502 if Brreg is down."""
    try:
        company = await client.get_company(orgnr)
    except (BrregUnavailable, httpx.HTTPError) as e:
        raise HTTPException(502, "Brønnøysundregistrene svarer ikke akkurat nå.") from e
    if company is None:
        raise HTTPException(404, f"Fant ikke {orgnr} i Enhetsregisteret.")
    return company.name


async def _rows(client: BrregClient, members: list[dict]) -> list[dict]:
    """Table rows with place, newest PDF year and latest key figures. Lookup errors → nulls."""
    slots = asyncio.Semaphore(ROW_CONCURRENCY)

    async def one(m: dict) -> dict:
        row = {
            "orgnr": m["orgnr"],
            "name": m["name"],
            "place": None,
            "last_pdf_year": None,
            "key_year": None,
            "currency": None,
            "aarsresultat": None,
            "ek_andel": None,
        }
        async with slots:
            try:
                company, years, figures = await asyncio.gather(
                    client.get_company(m["orgnr"]),
                    client.list_years(m["orgnr"]),
                    client.key_figures(m["orgnr"]),
                )
            except (BrregUnavailable, httpx.HTTPError) as e:
                log.warning("row lookup failed for %s: %r", m["orgnr"], e)
                return row
        if company:
            row["name"], row["place"] = company.name, company.place or None
        row["last_pdf_year"] = max(years) if years else None
        if figures:
            row.update(
                key_year=figures["year"],
                currency=figures["currency"],
                aarsresultat=figures["aarsresultat"],
                ek_andel=figures.get("ek_andel"),
            )
        return row

    return await asyncio.gather(*(one(m) for m in members))


# ---------- Favourites ----------


@router.get("/favourites")
async def favourites(
    rows: bool = False,
    email: str = Depends(get_user_email),
    client: BrregClient = Depends(get_client),
) -> dict:
    """Favourites as orgnr + name. rows=true adds Brreg's place, years and key figures (the table):
    that's three lookups per favourite, one on the rate-limited accounts register."""
    favs = db.list_favourites(email)
    return {"items": await _rows(client, favs) if rows else favs}


@router.put("/favourites/{orgnr}")
async def add_favourite(
    orgnr: str, email: str = Depends(get_user_email), client: BrregClient = Depends(get_client)
) -> dict:
    orgnr = _valid_orgnr(orgnr)
    db.add_favourite(email, orgnr, await _name_of(client, orgnr))
    return {"ok": True}


@router.delete("/favourites/{orgnr}")
async def remove_favourite(orgnr: str, email: str = Depends(get_user_email)) -> dict:
    db.remove_favourite(email, _valid_orgnr(orgnr))
    return {"ok": True}


# ---------- Recent ----------


@router.get("/recent")
async def recent(email: str = Depends(get_user_email)) -> dict:
    return {"items": db.list_recent(email)}


@router.delete("/recent/{orgnr}")
async def remove_recent(orgnr: str, email: str = Depends(get_user_email)) -> dict:
    db.remove_recent(email, _valid_orgnr(orgnr))
    return {"ok": True}


# ---------- Collections ----------


class NewCollection(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    orgnrs: list[str] = Field(default_factory=list, max_length=MAX_COLLECTION_SIZE)


class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _own(email: str, cid: int) -> dict:
    coll = db.get_collection(email, cid)
    if not coll:
        raise HTTPException(404, "Fant ikke samlingen.")
    return coll


@router.get("/collections")
async def collections(email: str = Depends(get_user_email)) -> dict:
    return {"items": db.list_collections(email)}


@router.post("/collections")
async def create_collection(
    body: NewCollection,
    email: str = Depends(get_user_email),
    client: BrregClient = Depends(get_client),
) -> dict:
    orgnrs = list(dict.fromkeys(_valid_orgnr(o.replace(" ", "")) for o in body.orgnrs))
    names = await asyncio.gather(*(_name_of(client, o) for o in orgnrs))
    name = body.name.strip()
    cid = db.create_collection(email, name, list(zip(orgnrs, names, strict=True)))
    return {"id": cid, "name": name}


@router.get("/collections/{cid}")
async def collection(
    cid: int, email: str = Depends(get_user_email), client: BrregClient = Depends(get_client)
) -> dict:
    coll = _own(email, cid)
    return {"id": coll["id"], "name": coll["name"], "items": await _rows(client, coll["members"])}


@router.patch("/collections/{cid}")
async def rename_collection(cid: int, body: Rename, email: str = Depends(get_user_email)) -> dict:
    if not db.rename_collection(email, cid, body.name.strip()):
        raise HTTPException(404, "Fant ikke samlingen.")
    return {"ok": True}


@router.delete("/collections/{cid}")
async def delete_collection(cid: int, email: str = Depends(get_user_email)) -> dict:
    if not db.delete_collection(email, cid):
        raise HTTPException(404, "Fant ikke samlingen.")
    return {"ok": True}


@router.put("/collections/{cid}/members/{orgnr}")
async def add_member(
    cid: int,
    orgnr: str,
    email: str = Depends(get_user_email),
    client: BrregClient = Depends(get_client),
) -> dict:
    coll = _own(email, cid)
    orgnr = _valid_orgnr(orgnr)
    if len(coll["members"]) >= MAX_COLLECTION_SIZE:
        raise HTTPException(400, f"Maks {MAX_COLLECTION_SIZE} selskaper per samling.")
    db.add_member(cid, orgnr, await _name_of(client, orgnr))
    return {"ok": True}


@router.delete("/collections/{cid}/members/{orgnr}")
async def remove_member(cid: int, orgnr: str, email: str = Depends(get_user_email)) -> dict:
    _own(email, cid)
    db.remove_member(cid, _valid_orgnr(orgnr))
    return {"ok": True}
