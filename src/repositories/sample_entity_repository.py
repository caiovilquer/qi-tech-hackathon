from datetime import date, datetime
from uuid import uuid4

from database import Context
from models import SampleEntity, SampleEntityStatus, SampleEntityStatusEvent


class SampleEntityRepository:
    """Queries only; the controller decides what to do with the results and when to commit."""

    def __init__(self, context: Context) -> None:
        self.session = context.db_session

    def create(self, sample_entity_data: dict) -> SampleEntity:
        sample_entity = SampleEntity()
        sample_entity.sample_entity_data = {"hello": sample_entity_data["hello"]}
        sample_entity.name = sample_entity_data["name"]
        sample_entity.email = sample_entity_data["email"]
        sample_entity.document_number = sample_entity_data["document_number"]
        sample_entity.birthdate = date.fromisoformat(sample_entity_data["birthdate"])
        sample_entity.sample_entity_key = str(uuid4())
        sample_entity.counter = 0
        sample_entity.status = self.get_status(SampleEntityStatus.CREATED)
        self.session.add(sample_entity)
        return sample_entity

    def update_status(self, sample_entity: SampleEntity, new_status_enumerator: str) -> None:
        new_status = self.get_status(new_status_enumerator)
        sample_entity.status = new_status
        new_status_event = SampleEntityStatusEvent()
        new_status_event.status = new_status
        new_status_event.event_datetime = datetime.now()
        sample_entity.status_events.append(new_status_event)

    def increment_counter(self, sample_entity: SampleEntity) -> None:
        sample_entity.counter = sample_entity.counter + 1

    def get_by_key(self, sample_entity_key: str) -> SampleEntity:
        return self.session.query(SampleEntity).filter(SampleEntity.sample_entity_key == sample_entity_key).first()

    def get_by_document_number(self, document_number: str) -> SampleEntity:
        return self.session.query(SampleEntity).filter(SampleEntity.document_number == document_number).first()

    def get_by_email(self, email: str) -> SampleEntity:
        return self.session.query(SampleEntity).filter(SampleEntity.email == email).first()

    def get_status(self, enumerator: str) -> SampleEntityStatus:
        return self.session.query(SampleEntityStatus).filter(SampleEntityStatus.enumerator == enumerator).one()

    def list_page(self, limit: int, offset: int, filters: dict) -> list:
        """One page, narrowed by every filter that was provided (filters combine with AND).

        Ordered by creation time with id as tiebreaker, so pages never repeat or skip rows.
        Fetches limit + 1 rows so the caller can tell whether a next page exists.
        """
        query = self.session.query(SampleEntity)
        status_enumerators = filters.get("status_enumerators")
        if status_enumerators:
            query = query.join(SampleEntity.status).filter(SampleEntityStatus.enumerator.in_(status_enumerators))
        name = filters.get("name")
        if name is not None:
            query = query.filter(SampleEntity.name.ilike(f"%{name}%"))
        email = filters.get("email")
        if email is not None:
            query = query.filter(SampleEntity.email == email)
        document_number = filters.get("document_number")
        if document_number is not None:
            query = query.filter(SampleEntity.document_number == document_number)
        birthdate_from = filters.get("birthdate_from")
        if birthdate_from is not None:
            query = query.filter(SampleEntity.birthdate >= birthdate_from)
        birthdate_to = filters.get("birthdate_to")
        if birthdate_to is not None:
            query = query.filter(SampleEntity.birthdate <= birthdate_to)
        query = query.order_by(SampleEntity.created_at.desc(), SampleEntity.id.desc())
        return query.limit(limit + 1).offset(offset).all()
