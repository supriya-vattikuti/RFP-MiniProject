"""Cohere API integration; API credentials are only held by the caller."""

from __future__ import annotations

from typing import Any

import cohere


DEFAULT_MODEL = "command-r-plus-08-2024"


class CohereEvaluator:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        if not api_key.strip():
            raise ValueError("Enter a Cohere API key before connecting.")
        self._client = cohere.ClientV2(api_key=api_key.strip())
        self.model = model

    def test_connection(self) -> None:
        self._client.models.list()

    def evaluate(self, prompt: str) -> str:
        return self.generate(prompt)

    def generate(self, prompt: str) -> str:
        response = self._client.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        content = response.message.content
        text = "".join(
            part.text for part in content if getattr(part, "text", None)
        )
        if not text.strip():
            raise ValueError("Cohere returned an empty evaluation response.")
        return text
