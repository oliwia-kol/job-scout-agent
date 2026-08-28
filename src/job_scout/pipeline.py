"""Explicit pipeline contracts; orchestration remains framework-independent."""

from __future__ import annotations

from typing import Protocol

from .domain import FitAssessment, JobOffer, JudgeResult, RawOffer


class Extractor(Protocol):
    def extract(self, raw_offer: RawOffer) -> JobOffer: ...


class Evaluator(Protocol):
    def evaluate(self, offer: JobOffer) -> FitAssessment: ...


class Judge(Protocol):
    def review(self, offer: JobOffer, assessment: FitAssessment) -> JudgeResult: ...


class OfferPipeline:
    def __init__(self, extractor: Extractor, evaluator: Evaluator, judge: Judge) -> None:
        self.extractor = extractor
        self.evaluator = evaluator
        self.judge = judge

    def process(self, raw_offer: RawOffer) -> tuple[JobOffer, FitAssessment, JudgeResult]:
        offer = self.extractor.extract(raw_offer)
        assessment = self.evaluator.evaluate(offer)
        judgment = self.judge.review(offer, assessment)
        return offer, assessment, judgment
