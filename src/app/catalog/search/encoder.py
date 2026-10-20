from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np
from fastembed import TextEmbedding
from huggingface_hub import set_client_factory, snapshot_download

from app.integrations.tls import system_tls_context

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
REPOSITORY = "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
REVISION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"


class Encoder(Protocol):
    """Заменяемый синхронный encoder; inference выполняется вне event loop."""

    identity: str

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """Вычисляет нормализованные векторы текстов.

        :param texts: Короткие документы или запросы
        :return: Матрица float32
        """
        ...


class SentenceEncoder:
    """Локальная многоязычная ONNX-модель фиксированной ревизии."""

    identity = f"{MODEL}@{REVISION}:fastembed-0.7.4:mean:normalized"

    def __init__(self, cache: Path, threads: int = 2) -> None:
        cache.mkdir(parents=True, exist_ok=True)
        set_client_factory(self._download_client)
        args = dict(
            repo_id=REPOSITORY,
            revision=REVISION,
            cache_dir=str(cache),
            allow_patterns=["*.json", "model_optimized.onnx"],
        )
        try:
            snapshot = snapshot_download(**args, local_files_only=True)
            if not all(
                (Path(snapshot) / name).is_file()
                for name in [
                    "model_optimized.onnx",
                    "tokenizer.json",
                    "config.json",
                    "tokenizer_config.json",
                    "special_tokens_map.json",
                ]
            ):
                raise FileNotFoundError("Model weights missing")
        except (FileNotFoundError, OSError):
            snapshot = snapshot_download(**args)
        self.model = TextEmbedding(
            MODEL, specific_model_path=snapshot, threads=threads, providers=["CPUExecutionProvider"]
        )

    @staticmethod
    def _download_client() -> httpx.Client:
        """Использует системное хранилище CA без отключения проверки TLS.

        :return: HTTP-клиент загрузки Hugging Face
        """
        return httpx.Client(verify=system_tls_context(), follow_redirects=True, timeout=60)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """Кодирует тексты батчами; query-векторы не кэшируются.

        :param texts: Поисковые тексты
        :return: Нормализованная матрица
        """
        matrix = np.asarray(list(self.model.embed(list(texts), batch_size=8)), dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if not np.isfinite(matrix).all() or (norms <= 0).any():
            raise ValueError("Invalid embedding matrix")
        return matrix / norms
