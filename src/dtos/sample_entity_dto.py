from copy import deepcopy
from typing import List

from models import SampleEntity


class SampleEntityDTO:
    """Translates the database object into the JSON the API returns."""

    @staticmethod
    def obj_to_dict(sample_entity: SampleEntity) -> dict:
        """Full entity with its status history."""
        sample_entity_dto = SampleEntityDTO.obj_to_simplified_dict(sample_entity)
        sample_entity_dto["status_events"] = [
            {"status": status_event.status.enumerator, "event_datetime": status_event.event_datetime.isoformat()}
            for status_event in sample_entity.status_events
        ]
        return sample_entity_dto

    @staticmethod
    def obj_to_simplified_dict(sample_entity: SampleEntity) -> dict:
        """Entity without the status history; used by listings to avoid one query per row."""
        dto = deepcopy(sample_entity.sample_entity_data)  # never mutate the object still used by the request
        dto["sample_entity_key"] = sample_entity.sample_entity_key
        dto["name"] = sample_entity.name
        dto["email"] = sample_entity.email
        dto["document_number"] = sample_entity.document_number
        dto["birthdate"] = sample_entity.birthdate.isoformat()
        dto["status"] = sample_entity.status.enumerator
        dto["counter"] = sample_entity.counter
        return dto

    @staticmethod
    def list_obj_to_list_dict(sample_entities_list: List[SampleEntity]) -> List[dict]:
        return [SampleEntityDTO.obj_to_simplified_dict(sample_entity) for sample_entity in sample_entities_list]

    @staticmethod
    def only_obj_key(sample_entity: SampleEntity) -> dict:
        return {"sample_entity_key": sample_entity.sample_entity_key}
