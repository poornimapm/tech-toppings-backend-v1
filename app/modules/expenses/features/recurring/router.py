from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.modules.expenses.features.entries.schemas import EntryOut
from app.modules.expenses.features.recurring.schemas import (
    DismissIn,
    MarkPaidIn,
    RuleIn,
    RuleOut,
    RulePatch,
    SuggestionOut,
)
from app.modules.expenses.features.recurring.service import Recurring
from app.platform import ActorDep, Page, PageParamsDep, answer_once, error_responses

router = APIRouter(prefix="/recurring")


def recurring(ctx: ActorDep) -> Recurring:
    return Recurring(ctx)


RecurringDep = Annotated[Recurring, Depends(recurring)]


@router.get(
    "",
    summary="What repeats and my bills, soonest due first",
    responses=error_responses(401, 403),
)
async def list_rules(service: RecurringDep, params: PageParamsDep) -> Page[RuleOut]:
    items, total = await service.overview(params)
    return Page[RuleOut].build(items, params, total)


@router.get(
    "/suggestions",
    summary="What looks like it repeats in my entries, as rules to confirm",
    responses=error_responses(401, 403),
)
async def list_suggestions(service: RecurringDep) -> list[SuggestionOut]:
    return await service.suggestions()


@router.post(
    "/suggestions/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Say a suggestion isn't regular: it is not suggested again",
    responses=error_responses(401, 403),
)
async def dismiss_suggestion(body: DismissIn, service: RecurringDep) -> None:
    await service.dismiss(body.signature)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add something that repeats, or confirm a suggestion (once per signature)",
    responses=error_responses(401, 403),
)
async def add_rule(body: RuleIn, service: RecurringDep, response: Response) -> RuleOut:
    return answer_once(response, await service.create(body))


@router.patch(
    "/{rule_id}",
    summary="Change a rule (only what is sent); pausing stops its entries and reminders",
    responses=error_responses(401, 403, 404),
)
async def change_rule(rule_id: str, body: RulePatch, service: RecurringDep) -> RuleOut:
    return await service.update(rule_id, body)


@router.delete(
    "/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a rule (the entries it added stay)",
    responses=error_responses(401, 403, 404),
)
async def delete_rule(rule_id: str, service: RecurringDep) -> None:
    await service.delete(rule_id)


@router.post(
    "/{rule_id}/mark-paid",
    status_code=status.HTTP_201_CREATED,
    summary="Mark a bill's due date paid: its entry is added once per due date",
    responses=error_responses(401, 403, 404),
)
async def mark_paid(
    rule_id: str, body: MarkPaidIn, service: RecurringDep, response: Response
) -> EntryOut:
    return EntryOut.of(answer_once(response, await service.mark_paid(rule_id, body)))
