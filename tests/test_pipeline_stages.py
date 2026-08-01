"""Tests for all pipeline stages: data_pull → rag_retrieval → persona_reasoning → judge_synthesis.

All external dependencies (LLM APIs, vector store, disk cache, yfinance, FRED) are mocked.
Run with: pytest tests/test_pipeline_stages.py -v
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swing_trader.data_pull.block import build_numeric_block
from swing_trader.data_pull.node import data_pull_node
from swing_trader.rag.node import rag_retrieval_node
from swing_trader.reasoning.judge import judge_synthesis_node
from swing_trader.reasoning.node import persona_reasoning_node
from swing_trader.schemas.pipeline import (
    FOMCEvent,
    FundamentalsResult,
    Layer1Output,
    MacroDataPoint,
    MacroResult,
    NumericBlock,
    PersonaOutputs,
    QualItem,
    QualitativeBlock,
)
from swing_trader.state import PipelineState, initial_state


# ── Fixtures ─────────────────────────────────────────────────────────────────

TICKER = "AAPL"
WINDOW_START = date(2024, 1, 1)
WINDOW_END = date(2024, 3, 31)


@pytest.fixture
def base_state() -> PipelineState:
    return initial_state(
        ticker=TICKER,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        run_type="eval",
    )


@pytest.fixture
def mock_fundamentals() -> FundamentalsResult:
    return FundamentalsResult(
        ticker=TICKER,
        report_date=date(2024, 2, 1),
        eps_actual=1.52,
        eps_estimate=1.45,
        eps_surprise_pct=5.2,
        revenue_actual_b=94.9,
        revenue_estimate_b=92.1,
        revenue_surprise_pct=3.0,
        revenue_yoy_pct=2.1,
        gross_margin_pct=45.9,
        prior_gross_margin_pct=45.2,
        guidance_direction="raised",
        guidance_note="FY24 services revenue outlook raised",
        pe_trailing=29.3,
        pe_forward=27.1,
        price_at_start=185.33,
        fifty_two_high=199.62,
        fifty_two_low=143.15,
        data_gaps=[],
    )


@pytest.fixture
def mock_macro() -> MacroResult:
    return MacroResult(
        sector="Technology",
        series=[
            MacroDataPoint(
                series_id="DFF", label="Fed Funds Rate",
                value_start=5.33, value_end=5.33, unit="%",
            ),
            MacroDataPoint(
                series_id="T10Y2Y", label="10Y-2Y Spread",
                value_start=-0.35, value_end=-0.28, unit="%",
            ),
            MacroDataPoint(
                series_id="CPIAUCSL", label="CPI YoY",
                value_start=3.4, value_end=3.5, unit="%",
            ),
        ],
        fomc_in_window=True,
        fomc_event=FOMCEvent(
            meeting_date="2024-01-31",
            decision="held at 5.25-5.50%",
        ),
        data_gaps=[],
    )


@pytest.fixture
def mock_numeric_block() -> NumericBlock:
    return NumericBlock(
        ticker=TICKER,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        rendered_text="=== NUMERIC DATA: AAPL ===\nEPS actual: $1.52\nRevenue: $94.9B",
        data_gaps=[],
        sources_used=["yfinance", "FRED"],
        fundamentals_report_date=date(2024, 2, 1),
        macro_series_pulled=["DFF", "T10Y2Y", "CPIAUCSL"],
    )


@pytest.fixture
def mock_qual_items() -> list[QualItem]:
    return [
        QualItem(
            source_type="analyst", date="2024-02-02", source="Morgan Stanley",
            sentiment_label="bullish",
            sentiment_reason="Price target raised to $220",
            summary="Morgan Stanley raised AAPL PT from $200 to $220 on services growth.",
            relevance_score=0.92,
        ),
        QualItem(
            source_type="news", date="2024-01-18", source="Reuters",
            sentiment_label="bearish",
            sentiment_reason="iPhone shipments declined in China",
            summary="Apple iPhone shipments in China fell 3.7% amid Huawei competition.",
            relevance_score=0.87,
        ),
    ]


@pytest.fixture
def mock_qual_block(mock_qual_items) -> QualitativeBlock:
    return QualitativeBlock(
        ticker=TICKER,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        chunks_retrieved=12,
        chunks_used=2,
        items=mock_qual_items,
    )


def _make_llm_response(content: str) -> SimpleNamespace:
    """Build a fake OpenAI-style chat completion response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
    )


def _make_embed_response(embedding: list[float] | None = None) -> SimpleNamespace:
    """Build a fake OpenAI-style embedding response."""
    if embedding is None:
        embedding = [0.1] * 768
    return SimpleNamespace(data=[SimpleNamespace(embedding=embedding)])


# ── Stage 1: Data Pull ──────────────────────────────────────────────────────


class TestDataPullNode:
    @pytest.mark.asyncio
    async def test_data_pull_fetches_and_builds_block(
        self, base_state, mock_fundamentals, mock_macro
    ):
        with (
            patch("swing_trader.data_pull.node.disk_cache") as cache_mock,
            patch("swing_trader.data_pull.node.pull_fundamentals", new_callable=AsyncMock) as fund_mock,
            patch("swing_trader.data_pull.node.pull_macro") as macro_mock,
        ):
            cache_mock.get_quarter.return_value = None
            fund_mock.return_value = mock_fundamentals
            macro_mock.return_value = mock_macro

            result = await data_pull_node(base_state)

        assert result["numeric_block"] is not None
        block = result["numeric_block"]
        assert block.ticker == TICKER
        assert "yfinance" in block.sources_used
        assert "FRED" in block.sources_used
        assert len(block.data_gaps) == 0

    @pytest.mark.asyncio
    async def test_data_pull_uses_cache(self, base_state, mock_fundamentals, mock_macro):
        cached_data = {
            "fundamentals": mock_fundamentals.model_dump(mode="json"),
            "macro": mock_macro.model_dump(mode="json"),
        }
        with (
            patch("swing_trader.data_pull.node.disk_cache") as cache_mock,
            patch("swing_trader.data_pull.node.pull_fundamentals", new_callable=AsyncMock) as fund_mock,
            patch("swing_trader.data_pull.node.pull_macro") as macro_mock,
        ):
            cache_mock.get_quarter.return_value = cached_data
            result = await data_pull_node(base_state)

        fund_mock.assert_not_called()
        macro_mock.assert_not_called()
        assert result["numeric_block"] is not None

    @pytest.mark.asyncio
    async def test_data_pull_propagates_data_gaps(self, base_state):
        fund = FundamentalsResult.empty(TICKER)
        macro = MacroResult.empty()

        with (
            patch("swing_trader.data_pull.node.disk_cache") as cache_mock,
            patch("swing_trader.data_pull.node.pull_fundamentals", new_callable=AsyncMock) as fund_mock,
            patch("swing_trader.data_pull.node.pull_macro") as macro_mock,
        ):
            cache_mock.get_quarter.return_value = None
            fund_mock.return_value = fund
            macro_mock.return_value = macro

            result = await data_pull_node(base_state)

        assert len(result["warnings"]) > 0
        assert any("unavailable" in w for w in result["warnings"])


# ── Stage 1b: build_numeric_block unit test ──────────────────────────────────


class TestBuildNumericBlock:
    def test_renders_single_quarter(self, mock_fundamentals, mock_macro):
        quarters = [("2024Q1", WINDOW_START, WINDOW_END, mock_fundamentals, mock_macro)]
        block = build_numeric_block(TICKER, WINDOW_START, WINDOW_END, quarters)

        assert block.ticker == TICKER
        assert "AAPL" in block.rendered_text
        assert "$1.52" in block.rendered_text
        assert "yfinance" in block.sources_used
        assert "FRED" in block.sources_used
        assert "DFF" in block.macro_series_pulled
        assert block.fundamentals_report_date == date(2024, 2, 1)

    def test_renders_empty_data(self):
        fund = FundamentalsResult.empty(TICKER)
        macro = MacroResult.empty()
        quarters = [("2024Q1", WINDOW_START, WINDOW_END, fund, macro)]
        block = build_numeric_block(TICKER, WINDOW_START, WINDOW_END, quarters)

        assert "N/A" in block.rendered_text
        assert len(block.data_gaps) > 0


# ── Stage 2: RAG Retrieval ───────────────────────────────────────────────────


class TestRagRetrievalNode:
    @pytest.mark.asyncio
    async def test_rag_returns_qual_block(self, base_state, mock_qual_items):
        fake_qual_block = QualitativeBlock(
            ticker=TICKER,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            chunks_retrieved=12,
            chunks_used=2,
            items=mock_qual_items,
        )

        with patch("swing_trader.rag.node.retrieve", new_callable=AsyncMock) as retrieve_mock:
            retrieve_mock.return_value = fake_qual_block
            result = await rag_retrieval_node(base_state)

        assert result["qualitative_block"] is not None
        assert result["qualitative_block"].chunks_used == 2
        assert len(result["qualitative_block"].items) == 2

    @pytest.mark.asyncio
    async def test_rag_empty_block_adds_warning(self, base_state):
        empty_block = QualitativeBlock(
            ticker=TICKER,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            chunks_retrieved=5,
            chunks_used=0,
        )

        with patch("swing_trader.rag.node.retrieve", new_callable=AsyncMock) as retrieve_mock:
            retrieve_mock.return_value = empty_block
            result = await rag_retrieval_node(base_state)

        assert any("no relevant chunks" in w for w in result["warnings"])


# ── Stage 2b: Retriever internals ────────────────────────────────────────────


class TestRetrieverInternals:
    def test_to_qual_item(self):
        from swing_trader.rag.retriever import _to_qual_item

        raw = {
            "content": "Apple reported strong earnings beating expectations.",
            "metadata": {
                "source_type": "news",
                "date": "2024-02-01",
                "source": "Reuters",
                "sentiment_label": "bullish",
                "sentiment_reason": "Earnings beat",
            },
            "rerank_score": 0.85,
        }
        item = _to_qual_item(raw)
        assert item.source_type == "news"
        assert item.sentiment_label == "bullish"
        assert item.relevance_score == 0.85

    def test_truncate_to_budget(self):
        from swing_trader.rag.retriever import _truncate_to_budget

        items = [
            QualItem(
                source_type="analyst", date="2024-01-01", source="MS",
                sentiment_label="bullish", sentiment_reason="PT raised",
                summary="x" * 2000, relevance_score=0.9,
            ),
            QualItem(
                source_type="news", date="2024-01-02", source="Reuters",
                sentiment_label="neutral", sentiment_reason="Flat",
                summary="y" * 2000, relevance_score=0.8,
            ),
        ]
        result = _truncate_to_budget(items, max_tokens=600)
        assert len(result) == 1
        assert result[0].source_type == "analyst"

    def test_deduplicate(self):
        from swing_trader.rag.retriever import _deduplicate

        results = [
            {"metadata": {"file_path": "a.md"}, "rerank_score": 0.5},
            {"metadata": {"file_path": "a.md"}, "rerank_score": 0.9},
            {"metadata": {"file_path": "b.md"}, "rerank_score": 0.7},
        ]
        deduped = _deduplicate(results)
        assert len(deduped) == 2
        a_scores = [r["rerank_score"] for r in deduped if r["metadata"]["file_path"] == "a.md"]
        assert a_scores == [0.9]

    @pytest.mark.asyncio
    async def test_retrieve_falls_back_without_date_filter(self):
        """When date-filtered search returns nothing, retry without dates."""
        from swing_trader.rag.retriever import retrieve

        fallback_results = [
            {
                "content": "AAPL earnings beat expectations in Q3.",
                "metadata": {
                    "source_type": "news",
                    "date": "2023-08-01",
                    "source": "Reuters",
                    "sentiment_label": "bullish",
                    "sentiment_reason": "Earnings beat",
                    "file_path": "a.md",
                },
                "score": 0.8,
            },
        ]

        mock_store = MagicMock()
        # First call (with date filter) returns nothing; second (without) returns data
        mock_store.search.side_effect = [[], fallback_results]

        with (
            patch("swing_trader.rag.retriever.get_vector_store", return_value=mock_store),
            patch("swing_trader.rag.retriever._embed", new_callable=AsyncMock, return_value=[0.1] * 768),
            patch("swing_trader.rag.retriever._rerank", new_callable=AsyncMock,
                  side_effect=lambda q, results, top_n: [
                      {**r, "rerank_score": r.get("score", 0.5)} for r in results[:top_n]
                  ]),
        ):
            block = await retrieve("AAPL", date(2024, 1, 1), date(2024, 3, 31))

        assert mock_store.search.call_count == 2
        # First call has date filters
        first_filter = mock_store.search.call_args_list[0][0][1]
        assert "date_gte" in first_filter
        # Second call drops date filters
        second_filter = mock_store.search.call_args_list[1][0][1]
        assert "date_gte" not in second_filter
        assert "date_lte" not in second_filter
        assert "ticker" in second_filter

        assert block.chunks_retrieved == 1
        assert block.chunks_used == 1


# ── Stage 3: Persona Reasoning ──────────────────────────────────────────────


class TestPersonaReasoningNode:
    @pytest.mark.asyncio
    async def test_persona_fills_all_four(self, base_state, mock_numeric_block, mock_qual_block):
        base_state["numeric_block"] = mock_numeric_block
        base_state["qualitative_block"] = mock_qual_block

        persona_responses = {
            "bull": "AAPL beat earnings with strong services growth, upside to $220.",
            "bear": "China risk is real with 3.7% iPhone shipment decline.",
            "macro": "Fed holding rates steady provides stable backdrop for tech.",
            "technicals": "Trading near 52-week highs with elevated P/E of 29x.",
        }

        call_count = 0

        async def fake_create(**kwargs):
            nonlocal call_count
            system_msg = kwargs["messages"][0]["content"]
            for key, text in persona_responses.items():
                if key in system_msg.lower():
                    call_count += 1
                    return _make_llm_response(text)
            return _make_llm_response("Generic analysis output for testing.")

        mock_client = AsyncMock()
        mock_client.chat.completions.create = fake_create

        with patch("swing_trader.reasoning.node.get_llm_client", return_value=mock_client):
            result = await persona_reasoning_node(base_state)

        assert result["persona_outputs"] is not None
        po = result["persona_outputs"]
        assert po.bull != ""
        assert po.bear != ""
        assert po.macro != ""
        assert po.technicals != ""
        assert result["composed_prompt"] != ""

    @pytest.mark.asyncio
    async def test_persona_warns_on_empty_output(self, base_state, mock_numeric_block):
        base_state["numeric_block"] = mock_numeric_block
        base_state["qualitative_block"] = None

        async def fake_create(**kwargs):
            return _make_llm_response("")

        mock_client = AsyncMock()
        mock_client.chat.completions.create = fake_create

        with patch("swing_trader.reasoning.node.get_llm_client", return_value=mock_client):
            result = await persona_reasoning_node(base_state)

        assert any("empty output" in w for w in result["warnings"])


# ── Stage 4: Judge Synthesis ─────────────────────────────────────────────────


VALID_JUDGE_JSON = json.dumps({
    "direction": "bullish",
    "magnitude_bucket": "3-8%",
    "confidence": 0.72,
    "dominant_drivers": ["fundamental", "sentiment"],
    "invalidation_condition": "AAPL closes below $175 for 3 consecutive trading days",
    "hold_window_bucket": "weeks",
    "thesis": "Strong Q1 earnings beat with services momentum supports bullish stance despite China headwinds",
    "sided_with": ["bull", "technicals"],
    "sided_reasoning": "Bull case supported by concrete earnings data; technicals confirm momentum",
})


class TestJudgeSynthesisNode:
    @pytest.mark.asyncio
    async def test_judge_parses_valid_json(self, base_state):
        base_state["composed_prompt"] = "Test prompt"
        base_state["persona_outputs"] = PersonaOutputs(
            bull="Bullish analysis", bear="Bearish analysis",
            macro="Macro analysis", technicals="Technical analysis",
        )

        async def fake_create(**kwargs):
            return _make_llm_response(VALID_JUDGE_JSON)

        mock_client = AsyncMock()
        mock_client.chat.completions.create = fake_create

        with patch("swing_trader.reasoning.judge.get_judge_client", return_value=mock_client):
            result = await judge_synthesis_node(base_state)

        l1 = result["layer1_output"]
        assert l1 is not None
        assert l1.direction == "bullish"
        assert l1.confidence == 0.72
        assert l1.magnitude_bucket == "3-8%"
        assert "fundamental" in l1.dominant_drivers
        assert len(l1.invalidation_condition) >= 20

    @pytest.mark.asyncio
    async def test_judge_handles_json_with_surrounding_text(self, base_state):
        base_state["composed_prompt"] = "Test prompt"
        base_state["persona_outputs"] = PersonaOutputs(
            bull="Bull", bear="Bear", macro="Macro", technicals="Tech",
        )

        wrapped = f"Here is my analysis:\n{VALID_JUDGE_JSON}\nEnd of response."

        async def fake_create(**kwargs):
            return _make_llm_response(wrapped)

        mock_client = AsyncMock()
        mock_client.chat.completions.create = fake_create

        with patch("swing_trader.reasoning.judge.get_judge_client", return_value=mock_client):
            result = await judge_synthesis_node(base_state)

        assert result["layer1_output"] is not None
        assert result["layer1_output"].direction == "bullish"

    @pytest.mark.asyncio
    async def test_judge_fails_gracefully_on_bad_json(self, base_state):
        base_state["composed_prompt"] = "Test prompt"
        base_state["persona_outputs"] = PersonaOutputs()

        async def fake_create(**kwargs):
            return _make_llm_response("I cannot produce valid JSON right now.")

        mock_client = AsyncMock()
        mock_client.chat.completions.create = fake_create

        with patch("swing_trader.reasoning.judge.get_judge_client", return_value=mock_client):
            result = await judge_synthesis_node(base_state)

        assert result["layer1_output"] is None
        assert any("failed" in w.lower() for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_judge_includes_thesis_hint(self, base_state):
        base_state["composed_prompt"] = "Test prompt"
        base_state["thesis_hint"] = "I think AAPL will rally on services growth"
        base_state["persona_outputs"] = PersonaOutputs(
            bull="Bull", bear="Bear", macro="Macro", technicals="Tech",
        )

        captured_messages = []

        async def fake_create(**kwargs):
            captured_messages.extend(kwargs["messages"])
            return _make_llm_response(VALID_JUDGE_JSON)

        mock_client = AsyncMock()
        mock_client.chat.completions.create = fake_create

        with patch("swing_trader.reasoning.judge.get_judge_client", return_value=mock_client):
            await judge_synthesis_node(base_state)

        user_msg = captured_messages[1]["content"]
        assert "THESIS HINT" in user_msg
        assert "services growth" in user_msg


# ── Schema validation ────────────────────────────────────────────────────────


class TestSchemaValidation:
    def test_layer1_rejects_vague_invalidation(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="too vague"):
            Layer1Output(
                direction="bullish",
                magnitude_bucket="3-8%",
                confidence=0.7,
                dominant_drivers=["fundamental"],
                invalidation_condition="if things go wrong with uncertainty",
                hold_window_bucket="weeks",
                thesis="A sufficiently long thesis statement for testing purposes here.",
            )

    def test_layer1_accepts_concrete_invalidation(self):
        l1 = Layer1Output(
            direction="bearish",
            magnitude_bucket="0-3%",
            confidence=0.55,
            dominant_drivers=["macro"],
            invalidation_condition="CPI prints below 3.0% in the next monthly report",
            hold_window_bucket="quarter",
            thesis="Macro headwinds from persistent inflation above Fed target support bearish view.",
        )
        assert l1.direction == "bearish"

    def test_layer1_rejects_duplicate_drivers(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="duplicates"):
            Layer1Output(
                direction="bullish",
                magnitude_bucket="3-8%",
                confidence=0.7,
                dominant_drivers=["fundamental", "fundamental"],
                invalidation_condition="AAPL drops below $150 support level on high volume",
                hold_window_bucket="weeks",
                thesis="Strong fundamentals support a bullish view on this stock right now.",
            )

    def test_persona_agreement_ratio(self):
        po = PersonaOutputs(
            bull="Very bullish outlook with strong upside potential",
            bear="Bearish risks from China exposure",
            macro="Neutral macro backdrop",
            technicals="Bullish momentum signals",
        )
        ratio = po.agreement_ratio("bullish")
        assert ratio >= 0.5  # bull and technicals match

    def test_qualitative_block_render(self, mock_qual_items):
        block = QualitativeBlock(
            ticker=TICKER,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            chunks_retrieved=12,
            chunks_used=2,
            items=mock_qual_items,
        )
        rendered = block.render()
        assert "QUALITATIVE CONTEXT" in rendered
        assert "ANALYST" in rendered
        assert "NEWS" in rendered
        assert "Morgan Stanley" in rendered

    def test_qualitative_block_render_empty(self):
        block = QualitativeBlock(
            ticker=TICKER,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            chunks_retrieved=0,
            chunks_used=0,
        )
        rendered = block.render()
        assert "No relevant qualitative context" in rendered


# ── Composed prompt builder ──────────────────────────────────────────────────


class TestComposedPrompt:
    def test_build_with_both_blocks(self, mock_numeric_block, mock_qual_block):
        from swing_trader.reasoning.prompts import build_composed_prompt

        prompt = build_composed_prompt(mock_numeric_block, mock_qual_block)
        assert "NUMERIC DATA" in prompt or "EPS" in prompt
        assert "QUALITATIVE CONTEXT" in prompt

    def test_build_with_no_data(self):
        from swing_trader.reasoning.prompts import build_composed_prompt

        prompt = build_composed_prompt(None, None)
        assert "No numeric data" in prompt
        assert "No qualitative context" in prompt

    def test_build_with_thesis_hint(self, mock_numeric_block):
        from swing_trader.reasoning.prompts import build_composed_prompt

        prompt = build_composed_prompt(mock_numeric_block, None, thesis_hint="Services growth")
        assert "Services growth" in prompt
        assert "THESIS HINT" in prompt


# ── Integration: full pipeline with all mocks ────────────────────────────────


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_end_to_end_pipeline(self, mock_fundamentals, mock_macro):
        """Run all 4 stages sequentially with mocked externals."""
        state = initial_state(
            ticker=TICKER,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            run_type="eval",
        )

        # Stage 1: data pull
        with (
            patch("swing_trader.data_pull.node.disk_cache") as cache_mock,
            patch("swing_trader.data_pull.node.pull_fundamentals", new_callable=AsyncMock) as fund_mock,
            patch("swing_trader.data_pull.node.pull_macro") as macro_mock,
        ):
            cache_mock.get_quarter.return_value = None
            fund_mock.return_value = mock_fundamentals
            macro_mock.return_value = mock_macro
            state = await data_pull_node(state)

        assert state["numeric_block"] is not None

        # Stage 2: rag retrieval
        fake_qual_block = QualitativeBlock(
            ticker=TICKER,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            chunks_retrieved=8,
            chunks_used=2,
            items=[
                QualItem(
                    source_type="analyst", date="2024-02-02", source="MS",
                    sentiment_label="bullish", sentiment_reason="PT raised",
                    summary="Analyst raised price target.", relevance_score=0.9,
                ),
                QualItem(
                    source_type="news", date="2024-01-18", source="Reuters",
                    sentiment_label="bearish", sentiment_reason="China risk",
                    summary="iPhone shipments declining.", relevance_score=0.85,
                ),
            ],
        )
        with patch("swing_trader.rag.node.retrieve", new_callable=AsyncMock) as retrieve_mock:
            retrieve_mock.return_value = fake_qual_block
            state = await rag_retrieval_node(state)

        assert state["qualitative_block"] is not None

        # Stage 3: persona reasoning
        persona_texts = {
            "bull": "Strong earnings beat supports upside to $220.",
            "bear": "China iPhone decline is a material risk.",
            "macro": "Stable rates support tech valuations.",
            "technicals": "Near 52-week high, P/E elevated at 29x.",
        }

        async def persona_create(**kwargs):
            system = kwargs["messages"][0]["content"]
            for key, text in persona_texts.items():
                if key in system.lower():
                    return _make_llm_response(text)
            return _make_llm_response("Fallback analysis.")

        persona_client = AsyncMock()
        persona_client.chat.completions.create = persona_create

        with patch("swing_trader.reasoning.node.get_llm_client", return_value=persona_client):
            state = await persona_reasoning_node(state)

        assert state["persona_outputs"] is not None
        assert all(
            getattr(state["persona_outputs"], p)
            for p in ("bull", "bear", "macro", "technicals")
        )

        # Stage 4: judge synthesis
        async def judge_create(**kwargs):
            return _make_llm_response(VALID_JUDGE_JSON)

        judge_client = AsyncMock()
        judge_client.chat.completions.create = judge_create

        with patch("swing_trader.reasoning.judge.get_judge_client", return_value=judge_client):
            state = await judge_synthesis_node(state)

        # Final assertions
        l1 = state["layer1_output"]
        assert l1 is not None
        assert l1.direction in ("bullish", "bearish", "neutral")
        assert 0.0 <= l1.confidence <= 1.0
        assert len(l1.dominant_drivers) >= 1
        assert len(l1.thesis) >= 30
        assert state["judge_reasoning"] != ""
