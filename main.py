import uvicorn

from lead_engine.api import create_app
from lead_engine.engine import RuleEngine

app = create_app(RuleEngine())


@app.get("/")
def read_root():
    return {"service": "lead-scoring-rule-engine"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
