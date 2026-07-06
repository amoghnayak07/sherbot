import logging
import sys

from fastapi import FastAPI
from sentence_transformers import SentenceTransformer

import config

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
log = logging.getLogger("embeddings")

app = FastAPI()
model = SentenceTransformer(config.EMBEDDING_MODEL)


@app.post("/embed")
async def embed(payload: dict) -> dict:
    texts = payload["texts"]
    embeddings = model.encode(texts).tolist()
    return {"embeddings": embeddings}


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("service:app", host="0.0.0.0", port=config.PORT, reload=True)
