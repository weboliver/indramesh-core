import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserProfile
from app.schemas.crud import ProfileUpdate, PasswordChange
from app.services.auth_service import verify_password, get_password_hash
from app.services.translation_service import _resolve_language


async def get_or_create_profile(db: AsyncSession, user_id: uuid.UUID) -> UserProfile:
    result = await db.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    profile = result.scalar_one_or_none()
    if profile is not None:
        return profile

    profile = UserProfile(user_id=user_id)
    db.add(profile)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        result = await db.execute(select(UserProfile).where(UserProfile.user_id == user_id))
        profile = result.scalar_one_or_none()
        if profile is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return profile

    await db.refresh(profile)
    return profile


async def update_profile(
    db: AsyncSession, user_id: uuid.UUID, profile_update: ProfileUpdate
) -> UserProfile:
    profile = await get_or_create_profile(db, user_id)

    if profile_update.first_name is not None:
        profile.first_name = profile_update.first_name
    if profile_update.last_name is not None:
        profile.last_name = profile_update.last_name
    if profile_update.avatar_url is not None:
        profile.avatar_url = profile_update.avatar_url
    if profile_update.ui_language is not None:
        await _resolve_language(db, profile_update.ui_language)
        profile.ui_language = profile_update.ui_language

    await db.commit()
    await db.refresh(profile)
    return profile


async def change_password(
    db: AsyncSession, user_id: uuid.UUID, password_change: PasswordChange
) -> None:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if not verify_password(password_change.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current password is incorrect",
        )

    user.password_hash = get_password_hash(password_change.new_password)
    await db.commit()
