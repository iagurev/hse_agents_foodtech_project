import gzip
import hashlib
import json
import sqlite3

import pytest

from app.catalog import Catalog
from app.catalog_check import check_catalog


async def test_three_file_catalog_is_readonly_and_state_is_separate(catalog, store) -> None:
    await catalog.validate()
    before = {p: p.read_bytes() for p in [catalog.recipes_path, catalog.products_path, catalog.sources_path]}
    async with catalog.connection() as db:
        for table in ["recipes", "products", "catalog_skus"]:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                await db.execute(f"DELETE FROM {table}")
    await store.set_questionnaire("u", {"goal": "health"})
    await store.initialize()
    async with store.connection() as db:
        names = {r[0] for r in await (await db.execute("SELECT name FROM sqlite_master")).fetchall()}
        assert not names & {"recipes", "products", "catalog_skus", "product_fts"}
    assert before == {p: p.read_bytes() for p in before}
    assert (await store.person("u")).questionnaire["goal"] == "health"


async def test_missing_database_is_reported_without_creating_empty_file(catalog, tmp_path) -> None:
    absent = tmp_path / "missing.sqlite"
    bad = Catalog(catalog.recipes_path, catalog.products_path, absent)
    with pytest.raises(ValueError, match="не найдена"):
        await bad.validate()
    assert not absent.exists()


async def test_mixed_release_is_rejected(catalog) -> None:
    with sqlite3.connect(catalog.products_path) as db:
        db.execute("UPDATE metadata SET value='another-release' WHERE key='release_id'")
    with pytest.raises(ValueError, match="разным выпускам"):
        await catalog.validate()


async def test_native_cards_and_document_keys_are_read_from_sources(catalog, product) -> None:
    card = {"id": 7, "price": {"current": 100}, "properties": [{"name": "Original"}]}
    document = "Исходное подтверждение".encode()
    with sqlite3.connect(catalog.sources_path) as db:
        db.execute("INSERT INTO catalog_skus VALUES(?,?)", ("7", json.dumps(card)))
        db.execute(
            "INSERT INTO documents VALUES(?,?,?,?)",
            ("data/old/page.html", gzip.compress(document), "gzip", hashlib.sha256(document).hexdigest()),
        )
        db.execute(
            "INSERT INTO document_aliases VALUES(?,?)", ("data/previous/page.html", "data/old/page.html")
        )
    with sqlite3.connect(catalog.products_path) as db:
        db.execute("INSERT INTO product_skus VALUES(?,?)", (product.product_id, "7"))
    assert await catalog.source_skus(product.product_id) == [card]
    assert await catalog.source_document("data/previous/page.html") == document
    assert await catalog.source_document("../../.env") is None
    report = await check_catalog(catalog)
    assert report["source_documents_checked"] == 1
    assert report["source_counts"]["catalog_skus"] == 1


async def test_checker_rejects_broken_cross_file_sku_reference(catalog, product) -> None:
    with sqlite3.connect(catalog.products_path) as db:
        db.execute("INSERT INTO product_skus VALUES(?,?)", (product.product_id, "missing"))
    with pytest.raises(ValueError, match="между финальными"):
        await check_catalog(catalog)


@pytest.mark.parametrize("field", ["nutrition_observations", "price_observations"])
async def test_checker_rejects_mass_volume_observation_mix(catalog, product, field: str) -> None:
    evidence = {field: [{"unit": "ml" if product.unit == "g" else "g", "base_amount": 100}]}
    with sqlite3.connect(catalog.products_path) as db:
        db.execute("UPDATE product_directory SET evidence=?", (json.dumps(evidence),))
    with pytest.raises(ValueError, match="Единица наблюдения"):
        await check_catalog(catalog)
    evidence[field][0]["unit"] = product.unit
    with sqlite3.connect(catalog.products_path) as db:
        db.execute("UPDATE product_directory SET evidence=?", (json.dumps(evidence),))
    assert (await check_catalog(catalog))["product_observations_checked"] == 1


async def test_checker_rejects_published_recipe_differing_from_saved_review(catalog) -> None:
    with sqlite3.connect(catalog.sources_path) as db:
        db.execute("INSERT INTO recipe_sources VALUES(1,'{}')")
        payload = {"payload": {"recipe_id": "r1", "title": "Другой состав"}}
        db.execute("INSERT INTO recipe_preparation VALUES(1,'ready','r1',?)", (json.dumps(payload),))
    with pytest.raises(ValueError, match="отличается от проверенного"):
        await check_catalog(catalog)


async def test_checker_rejects_stale_totals_in_existing_recipe(catalog) -> None:
    with sqlite3.connect(catalog.recipes_path) as db:
        row = db.execute("SELECT recipe_id,payload FROM recipes LIMIT 1").fetchone()
        recipe = json.loads(row[1])
        recipe["nutrition"]["kcal"] += 10
        db.execute("UPDATE recipes SET payload=? WHERE recipe_id=?", (json.dumps(recipe), row[0]))
    with pytest.raises(ValueError, match="Ошибка расчёта nutrition.kcal"):
        await check_catalog(catalog)


@pytest.mark.parametrize("field", ["nutrition_observations", "price_observations", "nutrition_supplement"])
async def test_checker_rejects_wrong_reference_base(catalog, product, field: str) -> None:
    observation = {"unit": product.unit, "base_amount": product.base_amount * 10}
    if field == "nutrition_supplement":
        evidence = {field: {f"nutrition_{key}": value for key, value in observation.items()}}
    else:
        evidence = {field: [observation]}
    with sqlite3.connect(catalog.products_path) as db:
        db.execute("UPDATE product_directory SET evidence=?", (json.dumps(evidence),))
    with pytest.raises(ValueError, match="База"):
        await check_catalog(catalog)


async def test_checker_accepts_only_rounding_of_reviewed_recipe(catalog_writer, recipe, product):
    data = recipe.model_dump()
    data["recipe_id"] = "recipe_adapt_vv_test"
    data["cost"]["mean_rub"] = 20.004
    data["nutrition"]["kcal"] = 180.001
    from app.models import Recipe

    rounded = Recipe.model_validate(data)
    catalog = catalog_writer([product], [rounded])
    with sqlite3.connect(catalog.sources_path) as db:
        db.execute("INSERT INTO recipe_sources VALUES(1,'{}')")
        db.execute(
            "INSERT INTO recipe_preparation VALUES(1,'ready',?,?)",
            (rounded.recipe_id, json.dumps({"payload": data})),
        )
    assert (await check_catalog(catalog))["recipes"] == 1
    with sqlite3.connect(catalog.recipes_path) as db:
        corrupted = rounded.model_dump()
        corrupted["nutrition"]["kcal"] += 1
        db.execute("UPDATE recipes SET payload=?", (json.dumps(corrupted),))
    with pytest.raises(ValueError, match="Ошибка расчёта"):
        await check_catalog(catalog)


async def test_sku_packages_use_accepted_evidence_without_overwriting_live_data(catalog, product):
    from app.models import Product

    facts = [
        {"sku_id": "7", "package_amount": 10, "package_unit": "g", "basis": "publisher page"},
        {"sku_id": "8", "package_amount": 10, "package_unit": "g", "basis": "publisher page"},
        {"sku_id": "8", "package_amount": 20, "package_unit": "g", "basis": "other page"},
        {"sku_id": "9", "package_amount": 30, "package_unit": "g", "basis": "unaccepted SKU"},
    ]
    with sqlite3.connect(catalog.products_path) as db:
        db.execute("UPDATE product_directory SET evidence=?", (json.dumps({"price_observations": facts}),))
        db.executemany(
            "INSERT INTO product_skus VALUES(?,?)", [(product.product_id, sku) for sku in ["7", "8"]]
        )

    def sku(id, **kwargs):
        return Product(sku_id=id, title="Специя", price_rub=123, **kwargs)

    source = [
        sku("7"),
        sku("8"),
        sku("9"),
        sku("7", package_amount=15, package_unit="g"),
        sku("7", package_amount=10, package_unit="pcs"),
    ]
    result = await catalog.enrich_skus(source)
    assert result[0].package_amount == 10 and result[0].package_unit == "g"
    assert result[0].package_basis == "Справочник SKU: publisher page"
    assert all(p.price_rub == 123 for p in result)
    assert result[1:] == source[1:]
    assert source[0].package_amount is None
    assert await catalog.enrich_skus(result) == result
