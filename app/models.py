from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Platform account and asset models.
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gitea_user_id: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    username: Mapped[str] = mapped_column(Text(collation="NOCASE"), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now_naive)

    sprites: Mapped[list["Sprite"]] = relationship(back_populates="owner")
    packs: Mapped[list["Pack"]] = relationship(back_populates="owner")
    favorite_folders: Mapped[list["FavoriteFolder"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", passive_deletes=True
    )


class RevokedAccessToken(Base):
    __tablename__ = "revoked_access_tokens"
    __table_args__ = (Index("idx_revoked_access_tokens_expires_at", "expires_at"),)

    jti: Mapped[str] = mapped_column(Text, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Sprite(Base):
    __tablename__ = "sprites"
    __table_args__ = (
        CheckConstraint("length(image_data) = 4096", name="ck_sprites_image_data_length"),
        Index("idx_sprites_created_at", "created_at"),
        Index("idx_sprites_owner_id", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    image_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now_naive)

    owner: Mapped[User | None] = relationship(back_populates="sprites")
    pack_links: Mapped[list["PackSprite"]] = relationship(
        back_populates="sprite", passive_deletes=True
    )
    favorite_links: Mapped[list["FavoriteFolderSprite"]] = relationship(
        back_populates="sprite", passive_deletes=True
    )


class Pack(Base):
    __tablename__ = "packs"
    __table_args__ = (Index("idx_packs_owner_id", "owner_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now_naive)

    owner: Mapped[User | None] = relationship(back_populates="packs")
    sprite_links: Mapped[list["PackSprite"]] = relationship(
        back_populates="pack",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PackSprite.position",
    )


class PackSprite(Base):
    __tablename__ = "pack_sprites"
    __table_args__ = (
        UniqueConstraint("pack_id", "sprite_id", name="uq_pack_sprites_pack_sprite"),
        CheckConstraint("position >= 0", name="ck_pack_sprites_position_nonnegative"),
        Index("idx_pack_sprites_sprite_id", "sprite_id"),
    )

    pack_id: Mapped[int] = mapped_column(
        ForeignKey("packs.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    sprite_id: Mapped[int] = mapped_column(
        ForeignKey("sprites.id", ondelete="CASCADE"), nullable=False
    )

    pack: Mapped[Pack] = relationship(back_populates="sprite_links")
    sprite: Mapped[Sprite] = relationship(back_populates="pack_links")


# Private favorite collection models.
class FavoriteFolder(Base):
    __tablename__ = "favorite_folders"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_favorite_folders_owner_name"),
        Index("idx_favorite_folders_owner_id", "owner_id"),
        Index("idx_favorite_folders_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text(collation="NOCASE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now_naive)

    owner: Mapped[User] = relationship(back_populates="favorite_folders")
    sprite_links: Mapped[list["FavoriteFolderSprite"]] = relationship(
        back_populates="folder",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="FavoriteFolderSprite.created_at.desc()",
    )


class FavoriteFolderSprite(Base):
    __tablename__ = "favorite_folder_sprites"
    __table_args__ = (
        Index("idx_favorite_folder_sprites_sprite_id", "sprite_id"),
        Index("idx_favorite_folder_sprites_created_at", "created_at"),
    )

    folder_id: Mapped[int] = mapped_column(
        ForeignKey("favorite_folders.id", ondelete="CASCADE"), primary_key=True
    )
    sprite_id: Mapped[int] = mapped_column(
        ForeignKey("sprites.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now_naive)

    folder: Mapped[FavoriteFolder] = relationship(back_populates="sprite_links")
    sprite: Mapped[Sprite] = relationship(back_populates="favorite_links")
