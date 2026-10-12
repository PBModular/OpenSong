from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import BigInteger, String, Integer, Boolean, Float, Text
from typing import Optional


class Base(DeclarativeBase):
    pass


class ChatSettings(Base):
    __tablename__ = "opensong_chat_settings"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    pin_song: Mapped[bool] = mapped_column(Boolean, default=True)
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    approver_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    included_genres: Mapped[str] = mapped_column(
        Text, default="pop,rock,electronic,hip-hop,indie,jazz,metal,r-n-b,classical,synthwave"
    )
    last_sent_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    next_run_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    def get_genres_list(self) -> list[str]:
        if not self.included_genres:
            return []
        return [g.strip().lower() for g in self.included_genres.split(",") if g.strip()]

    def set_genres_list(self, genres: list[str]) -> None:
        self.included_genres = ",".join(sorted(set(g.strip().lower() for g in genres if g.strip())))


class SongPost(Base):
    __tablename__ = "opensong_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(256))
    artist: Mapped[str] = mapped_column(String(256))
    genre: Mapped[str] = mapped_column(String(64))
    spotify_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    posted_at: Mapped[float] = mapped_column(Float)


class SongVote(Base):
    __tablename__ = "opensong_votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    vote: Mapped[str] = mapped_column(String(16))  # 'like' or 'dislike'
    voted_at: Mapped[float] = mapped_column(Float)


class PendingApproval(Base):
    __tablename__ = "opensong_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    dm_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    approver_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(256))
    artist: Mapped[str] = mapped_column(String(256))
    genre: Mapped[str] = mapped_column(String(64))
    spotify_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    audio_path: Mapped[str] = mapped_column(String(512))
    cover_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # 'pending', 'approved', 'rejected'
    created_at: Mapped[float] = mapped_column(Float)

