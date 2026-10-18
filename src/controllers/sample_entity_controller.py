from datetime import date

from controllers.base_controller import BaseController
from dtos import SampleEntityDTO
from errors import (
    DuplicatedDocumentNumber,
    DuplicatedEmail,
    InvalidBirthdate,
    InvalidDocumentNumber,
    InvalidParameter,
    NotFoundSampleEntity,
    SampleEntityFinalStatus,
    UnderageSampleEntity,
)
from models import SampleEntity, SampleEntityStatus
from repositories import SampleEntityRepository
from utils.document_number import is_valid_cpf

# Below the minimum the entity is refused; above the maximum it is created already in "failed".
MINIMUM_AGE = 18
MAXIMUM_AGE = 80


class SampleEntityController(BaseController):
    """Business rules of the Sample Entity. Commits happen here, after every rule has passed."""

    def __init__(self) -> None:
        super().__init__(__name__)
        self.sample_entity_repository = SampleEntityRepository(self.context)

    def create(self, sample_entity_data: dict) -> dict:
        self.logger.debug("sample_entity.create")
        document_number = sample_entity_data["document_number"]
        email = sample_entity_data["email"]
        birthdate = self._parse_birthdate(sample_entity_data["birthdate"])

        if not is_valid_cpf(document_number):
            raise InvalidDocumentNumber(document_number)
        if self.sample_entity_repository.get_by_document_number(document_number) is not None:
            raise DuplicatedDocumentNumber(document_number)
        if self.sample_entity_repository.get_by_email(email) is not None:
            raise DuplicatedEmail(email)

        age = self._age_in_years(birthdate)
        if age < MINIMUM_AGE:
            raise UnderageSampleEntity(age, MINIMUM_AGE)

        sample_entity = self.sample_entity_repository.create(sample_entity_data=sample_entity_data)
        new_status = SampleEntityStatus.FAILED if age > MAXIMUM_AGE else SampleEntityStatus.PENDING
        self.sample_entity_repository.update_status(sample_entity, new_status)

        sample_entity_dto = SampleEntityDTO.only_obj_key(sample_entity)
        self.session.commit()
        return sample_entity_dto

    def get_by_key(self, sample_entity_key: str) -> dict:
        self.logger.debug(f"sample_entity.get key={sample_entity_key}")
        sample_entity = self.sample_entity_repository.get_by_key(sample_entity_key)
        if sample_entity is None:
            raise NotFoundSampleEntity(sample_entity_key)
        return SampleEntityDTO.obj_to_dict(sample_entity)

    def get_list(self, limit: int, offset: int, filters: dict) -> dict:
        """Validates the filters that only make sense together, then returns one page."""
        birthdate_from = filters.get("birthdate_from")
        birthdate_to = filters.get("birthdate_to")
        if birthdate_from is not None:
            birthdate_from = self._parse_birthdate(birthdate_from)
            filters["birthdate_from"] = birthdate_from
        if birthdate_to is not None:
            birthdate_to = self._parse_birthdate(birthdate_to)
            filters["birthdate_to"] = birthdate_to
        if birthdate_from is not None and birthdate_to is not None and birthdate_from > birthdate_to:
            raise InvalidParameter(f"birthdate_from ({birthdate_from}) is after birthdate_to ({birthdate_to})")

        sample_entities_list = self.sample_entity_repository.list_page(limit, offset, filters)
        # One extra row is fetched only to know whether a next page exists.
        is_last_page = len(sample_entities_list) <= limit
        if not is_last_page:
            sample_entities_list = sample_entities_list[:-1]
        return {
            "sample_entities_list_dto": SampleEntityDTO.list_obj_to_list_dict(sample_entities_list),
            "is_last_page": is_last_page,
        }

    def update_status(self, sample_entity_key: str, new_status: str) -> dict:
        sample_entity = self.sample_entity_repository.get_by_key(sample_entity_key)
        if sample_entity is None:
            raise NotFoundSampleEntity(sample_entity_key)
        self._check_status_can_change(sample_entity, new_status)
        self.sample_entity_repository.update_status(sample_entity, new_status)
        sample_entity_dto = SampleEntityDTO.only_obj_key(sample_entity)
        self.session.commit()
        return sample_entity_dto

    def webhook_increment_counter(self, sample_entity_key: str) -> None:
        self.logger.debug(f"sample_entity.webhook key={sample_entity_key}")
        sample_entity = self.sample_entity_repository.get_by_key(sample_entity_key)
        if sample_entity is None:
            raise NotFoundSampleEntity(sample_entity_key)
        self.sample_entity_repository.increment_counter(sample_entity)
        self.session.commit()

    def _check_status_can_change(self, sample_entity: SampleEntity, new_status: str) -> None:
        old_status = sample_entity.status.enumerator
        if old_status != SampleEntityStatus.PENDING:
            raise SampleEntityFinalStatus(old_status, new_status)

    def _parse_birthdate(self, raw_birthdate: str) -> date:
        """The schema guarantees the format; the calendar is checked here (422 instead of 500)."""
        try:
            return date.fromisoformat(raw_birthdate)
        except ValueError:
            raise InvalidBirthdate(raw_birthdate)

    def _age_in_years(self, birthdate: date) -> int:
        today = date.today()
        age = today.year - birthdate.year
        if (today.month, today.day) < (birthdate.month, birthdate.day):
            age -= 1
        return age
