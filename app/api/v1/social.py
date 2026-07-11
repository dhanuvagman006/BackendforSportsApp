"""Friend requests and direct messages (spec §6.5–6.6)."""
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.db.base import get_db
from app.db.models import (
    Conversation,
    ConversationParticipant,
    FriendRequest,
    Friendship,
    Message,
    User,
)
from app.core.deps import Page
from app.services.notify import create_notification, deliver_notification
from app.services.serializers import avatar_url

router = APIRouter(tags=["social"])


# --------------------------------------------------------------------------- friend requests
class FriendRequestIn(BaseModel):
    to_id: uuid.UUID


def _pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if str(a) < str(b) else (b, a)


@router.post("/friend-requests", status_code=201)
async def send_friend_request(body: FriendRequestIn, background: BackgroundTasks,
                              db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    if body.to_id == user.id:
        raise bad_request("VALIDATION_ERROR", "You can't friend yourself.")
    target = await db.get(User, body.to_id)
    if target is None or target.deleted_at is not None:
        raise not_found("PLAYER_NOT_FOUND", "This user doesn't exist.")

    a, b = _pair(user.id, body.to_id)
    already = (
        await db.execute(select(Friendship.id).where(Friendship.user_a_id == a, Friendship.user_b_id == b))
    ).first()
    if already:
        raise conflict("ALREADY_FRIENDS", "You're already friends.")
    pending = (
        await db.execute(
            select(FriendRequest).where(
                FriendRequest.status == "pending",
                or_(
                    and_(FriendRequest.from_id == user.id, FriendRequest.to_id == body.to_id),
                    and_(FriendRequest.from_id == body.to_id, FriendRequest.to_id == user.id),
                ),
            )
        )
    ).scalar_one_or_none()
    if pending:
        raise conflict("REQUEST_PENDING", "A friend request is already pending between you two.")

    fr = FriendRequest(from_id=user.id, to_id=body.to_id)
    db.add(fr)
    await db.flush()
    n = await create_notification(db, body.to_id, "new_follower",
                                  f"{user.full_name} sent you a friend request",
                                  title="Friend Request", deep_link_id=str(user.id))
    await db.commit()
    background.add_task(deliver_notification, n.id)
    return {"id": str(fr.id), "status": fr.status, "created_at": fr.created_at.isoformat()}


async def _request_for_recipient(db: AsyncSession, request_id: uuid.UUID, user: User) -> FriendRequest:
    fr = await db.get(FriendRequest, request_id)
    if fr is None:
        raise not_found("PLAYER_NOT_FOUND", "Friend request not found.")
    if fr.to_id != user.id:
        raise forbidden("NOT_ALLOWED", "Only the recipient can respond to this request.")
    if fr.status != "pending":
        raise conflict("ALREADY_RESPONDED", "This request was already responded to.")
    return fr


@router.post("/friend-requests/{request_id}/accept")
async def accept_friend_request(request_id: uuid.UUID, background: BackgroundTasks,
                                db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    fr = await _request_for_recipient(db, request_id, user)
    fr.status = "accepted"
    a, b = _pair(fr.from_id, fr.to_id)
    db.add(Friendship(user_a_id=a, user_b_id=b))
    n = await create_notification(db, fr.from_id, "new_follower",
                                  f"{user.full_name} accepted your friend request",
                                  title="Friend Request Accepted", deep_link_id=str(user.id))
    await db.commit()
    background.add_task(deliver_notification, n.id)
    return {"id": str(fr.id), "status": "accepted"}


@router.post("/friend-requests/{request_id}/decline")
async def decline_friend_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                                 user: User = Depends(get_current_user)):
    fr = await _request_for_recipient(db, request_id, user)
    fr.status = "declined"
    await db.commit()
    return {"id": str(fr.id), "status": "declined"}


@router.get("/friend-requests")
async def list_friend_requests(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (
        await db.execute(
            select(FriendRequest, User)
            .join(User, User.id == FriendRequest.from_id)
            .where(FriendRequest.to_id == user.id, FriendRequest.status == "pending")
            .order_by(FriendRequest.created_at.desc())
        )
    ).all()
    return {
        "items": [
            {"id": str(fr.id), "from": {"id": str(u.id), "name": u.full_name, "avatar_url": avatar_url(u)},
             "created_at": fr.created_at.isoformat()}
            for fr, u in rows
        ]
    }


# --------------------------------------------------------------------------- conversations
class ConversationIn(BaseModel):
    participant_id: uuid.UUID


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationIn, db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    if body.participant_id == user.id:
        raise bad_request("VALIDATION_ERROR", "You can't message yourself.")
    other = await db.get(User, body.participant_id)
    if other is None or other.deleted_at is not None:
        raise not_found("PLAYER_NOT_FOUND", "This user doesn't exist.")

    mine = select(ConversationParticipant.conversation_id).where(ConversationParticipant.user_id == user.id)
    theirs = select(ConversationParticipant.conversation_id).where(ConversationParticipant.user_id == body.participant_id)
    existing = (
        await db.execute(select(Conversation.id).where(Conversation.id.in_(mine), Conversation.id.in_(theirs)))
    ).scalar_one_or_none()
    if existing:
        return {"conversation_id": str(existing), "created": False}

    conv = Conversation()
    db.add(conv)
    await db.flush()
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=user.id))
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=body.participant_id))
    await db.commit()
    return {"conversation_id": str(conv.id), "created": True}


async def _participant_or_403(db: AsyncSession, conversation_id: uuid.UUID, user: User):
    row = (
        await db.execute(
            select(ConversationParticipant.id).where(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.user_id == user.id,
            )
        )
    ).first()
    if row is None:
        raise forbidden("NOT_ALLOWED", "You're not part of this conversation.")


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: uuid.UUID, page: Page = Depends(),
                        db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await _participant_or_403(db, conversation_id, user)
    total = (
        await db.execute(select(func.count()).select_from(Message)
                         .where(Message.conversation_id == conversation_id))
    ).scalar_one()
    rows = (
        await db.execute(
            select(Message).where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc()).offset(page.offset).limit(page.limit)
        )
    ).scalars().all()
    items = [
        {"id": str(m.id), "sender_id": str(m.sender_id), "body": m.body,
         "mine": m.sender_id == user.id, "created_at": m.created_at.isoformat()}
        for m in rows
    ]
    return page.envelope(items, total)


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def send_message(conversation_id: uuid.UUID, body: MessageIn, background: BackgroundTasks,
                       db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await _participant_or_403(db, conversation_id, user)
    msg = Message(conversation_id=conversation_id, sender_id=user.id, body=body.body)
    db.add(msg)
    await db.flush()

    others = (
        await db.execute(
            select(ConversationParticipant.user_id).where(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.user_id != user.id,
            )
        )
    ).scalars().all()
    notif_ids = []
    for uid in others:
        n = await create_notification(db, uid, "new_message",
                                      f"{user.full_name}: {body.body[:80]}",
                                      deep_link_id=str(conversation_id))
        notif_ids.append(n.id)
    await db.commit()

    payload = {"id": str(msg.id), "sender_id": str(user.id), "body": msg.body,
               "created_at": msg.created_at.isoformat()}
    from app.realtime import manager
    background.add_task(manager.emit, f"conversation:{conversation_id}", "message", payload)
    for nid in notif_ids:
        background.add_task(deliver_notification, nid)
    return payload
