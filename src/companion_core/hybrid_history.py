"""Local keyword and vector ranking of already-authorized historical conversation turns."""
import logging
import os
import re
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from threading import Lock

from .retrieval import scores

logger = logging.getLogger(__name__)
MODEL_DIR = Path(__file__).with_name("history_model")
# A speaker preference applies to direct questions about oneself. General requests
# such as "帮我问问阿禾" and shared plans ("我们") keep the ordinary group ranking.
SELF_QUESTION = re.compile(r"(?:^|[，。？！、\s]|现在|上次说|之前说|记得)(?:我(?:自己|给|养|在|用|的|原来|之前|上次|叫|最))")


class EncoderUnavailable(Exception):
    pass


class BgeEncoder:
    def __init__(self, directory):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "model_quantized.onnx"), options,
                                            providers=["CPUExecutionProvider"])

    def encode(self, texts):
        batch = self.tokenizer.encode_batch(texts)
        fields = {"input_ids": "ids", "attention_mask": "attention_mask", "token_type_ids": "type_ids"}
        inputs = {item.name: self.np.asarray([getattr(t, fields[item.name]) for t in batch],
                                             dtype=self.np.int64) for item in self.session.get_inputs()}
        vectors = self.session.run(None, inputs)[0][:, 0]
        return vectors / self.np.maximum(self.np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)


@lru_cache(maxsize=1)
def shared_encoder(directory):
    # The model is read-only. Each runtime retains its own versioned record cache.
    return BgeEncoder(directory)


class HybridHistory:
    def __init__(self, encoder):
        self.encoder = encoder
        self.cache = OrderedDict()
        self.lock = Lock()

    @classmethod
    def configured(cls):
        override = os.getenv("QIBAN_HYBRID_HISTORY_DIR")
        if override == "off":
            return None
        try:
            return cls(shared_encoder(Path(override).resolve() if override else MODEL_DIR))
        except Exception as exc:
            logger.warning("Local history encoder unavailable (%s), using standard recall", type(exc).__name__)
            return None

    def encode(self, texts):
        try:
            return self.encoder.encode(texts)
        except Exception as exc:
            raise EncoderUnavailable(type(exc).__name__) from exc

    def __call__(self, records, query, queries=(), *, passages=None, current_sender=None):
        if not records:
            return {}
        sparse = scores(records, query, queries, passages=passages)
        dense = {}
        with self.lock:
            question = self.encode(["为这个句子生成表示以用于检索相关文章：" + query])[0]
            # Score cache hits first so inserting a miss cannot evict a later hit in this scan.
            missing = []
            for row in records:
                key = (row["kind"], row["data"]["id"], row["version"])
                if key in self.cache:
                    self.cache.move_to_end(key)
                    dense[key[:2]] = float(self.cache[key] @ question)
                else:
                    missing.append((key, row["data"]["content"]))
            for offset in range(0, len(missing), 32):
                batch = missing[offset:offset + 32]
                vectors = self.encode([content for _, content in batch])
                for (key, _), vector in zip(batch, vectors, strict=True):
                    dense[key[:2]] = float(vector @ question)
                    self.cache[key] = vector.copy()
                    if len(self.cache) > 2048:
                        self.cache.popitem(last=False)
        fused = {}
        for ranking in (sparse, dense):
            for rank, key in enumerate(sorted((k for k in ranking if ranking[k] > 0),
                                              key=lambda k: (-ranking[k], k)), 1):
                fused[key] = fused.get(key, 0.) + 1 / (60 + rank)
        if current_sender and SELF_QUESTION.search(query):
            # Only promote a speaker's topical hits. Unrelated messages by a prolific
            # asker must not fill the two evidence windows just because they wrote them.
            shortlist = sorted(fused, key=lambda key: (-fused[key], key))[:8]
            own = {(row["kind"], row["data"]["id"]) for row in records
                   if row["data"]["speaker"] == "user" and row["data"]["sender_uid"] == current_sender}
            for rank, key in enumerate((key for key in shortlist if key in own and sparse.get(key, 0) > 0), 1):
                fused[key] += 1 / (60 + rank)
        return fused
