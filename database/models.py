from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer
from database.database import Base

class NwasLogsheet(Base):
    __tablename__ = "nwas_logsheet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    run: Mapped[str] = mapped_column(String)
    jrny_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String)
    from_address: Mapped[str] = mapped_column(String)
    to_address: Mapped[str] = mapped_column(String)
    esc: Mapped[str] = mapped_column(String)
    notes: Mapped[str] = mapped_column(String)
    phone_number: Mapped[str] = mapped_column(String)
    formatted_time: Mapped[str] = mapped_column(String)
    cost_center: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)


class RebookJobs(Base):
    __tablename__ = "rebook_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    run: Mapped[str] = mapped_column(String)
    jrny_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String)
    from_address: Mapped[str] = mapped_column(String)
    to_address: Mapped[str] = mapped_column(String)
    esc: Mapped[str] = mapped_column(String)
    notes: Mapped[str] = mapped_column(String)
    phone_number: Mapped[str] = mapped_column(String)
    formatted_time: Mapped[str] = mapped_column(String)
    cost_center: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)


class UpdateLogsheet(Base):
    __tablename__ = "update_logsheet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    run: Mapped[str] = mapped_column(String)
    jrny_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String)
    from_address: Mapped[str] = mapped_column(String)
    to_address: Mapped[str] = mapped_column(String)
    esc: Mapped[str] = mapped_column(String)
    notes: Mapped[str] = mapped_column(String)
    phone_number: Mapped[str] = mapped_column(String)
    formatted_time: Mapped[str] = mapped_column(String)
    cost_center: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)

class AppMeta(Base):
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)