import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import numpy as np
from loguru import logger

from app.catalog.search.encoder import Encoder
from app.catalog.search.features import Language, equipment_features
from app.models import NormalizedProduct, Recipe

VERSION = "recipe-retrieval-v1"


def sha256(path: Path) -> str:
    """Считает отпечаток файла потоково.

    :param path: Проверяемый файл
    :return: SHA256
    """
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_index(
    paths: list[Path],
    cache: Path,
    recipes: list[Recipe],
    products: dict[str, NormalizedProduct],
    language: Language,
    encoder: Encoder | None,
) -> tuple[str, Path, list[dict], np.ndarray | None]:
    """Читает проверенный производный индекс либо публикует новую атомарную директорию.

    :param paths: Три исходные read-only БД
    :param cache: Директория производных артефактов вне data
    :param recipes: Опубликованные рецепты в устойчивом порядке
    :param products: Справочник ингредиентов
    :param language: Лемматизатор
    :param encoder: Необязательная локальная модель
    :return: ID, путь, документы и векторы индекса
    """
    cache = cache.resolve()
    fingerprint = {
        "version": VERSION,
        "databases": {str(p.resolve()): sha256(p) for p in paths},
        "encoder": encoder.identity if encoder else "lexical",
    }
    index_id = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    target = cache / index_id
    ids = [r.recipe_id for r in recipes]
    try:
        manifest = json.loads((target / "manifest.json").read_text())
        assert manifest["fingerprint"] == fingerprint and manifest["recipe_ids"] == ids
        for name, digest in manifest["artifacts"].items():
            assert sha256(target / name) == digest
        docs = json.loads((target / "documents.json").read_text())
        assert [d["recipe_id"] for d in docs] == ids
        matrix = np.load(target / "vectors.npy", allow_pickle=False) if encoder else None
        if matrix is not None:
            assert (
                matrix.ndim == 2
                and matrix.shape[0] == len(ids)
                and matrix.shape[1] > 0
                and np.isfinite(matrix).all()
            )
        with sqlite3.connect((target / "lexical.sqlite").as_uri() + "?mode=ro", uri=True) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert [r[0] for r in db.execute("SELECT recipe_id FROM search ORDER BY rowid")] == ids
        return index_id, target, docs, matrix
    except (OSError, ValueError, AssertionError, KeyError, sqlite3.Error):
        logger.info("Retrieval index rebuild started", recipe_count=len(recipes))
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as temporary:
        staging = Path(temporary)
        docs = []
        with sqlite3.connect(staging / "lexical.sqlite") as db:
            db.execute("CREATE VIRTUAL TABLE search USING fts5(recipe_id UNINDEXED,title,ingredients,method)")
            for recipe in recipes:
                names = [products[i.product_id].name for i in recipe.ingredients]
                method = " ".join(recipe.steps)[:1600]
                text = f"{recipe.title}. Ингредиенты: {', '.join(names)}. {method[:600]}"
                docs.append(
                    {
                        "recipe_id": recipe.recipe_id,
                        "text": text,
                        "features": equipment_features(recipe),
                        "title_terms": language.words(recipe.title),
                    }
                )
                db.execute(
                    "INSERT INTO search VALUES(?,?,?,?)",
                    (
                        recipe.recipe_id,
                        " ".join(language.words(recipe.title)),
                        " ".join(language.words(" ".join(names))),
                        " ".join(language.words(method)),
                    ),
                )
        (staging / "documents.json").write_text(json.dumps(docs, ensure_ascii=False, separators=(",", ":")))
        matrix = encoder.encode([d["text"] for d in docs]) if encoder and docs else None
        if matrix is not None:
            if matrix.ndim != 2 or matrix.shape[0] != len(ids) or not np.isfinite(matrix).all():
                raise ValueError("Invalid document embeddings")
            np.save(staging / "vectors.npy", matrix, allow_pickle=False)
        artifacts = {p.name: sha256(p) for p in staging.iterdir()}
        (staging / "manifest.json").write_text(
            json.dumps(
                {"fingerprint": fingerprint, "recipe_ids": ids, "artifacts": artifacts}, sort_keys=True
            )
        )
        # Один процесс обслуживает приложение; запросы начинаются только после prepare.
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    return index_id, target, docs, matrix
