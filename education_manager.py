import json
import os
from typing import List, Dict, Optional, Any

class EducationPoolManager:
    def __init__(self, storage_path: str = "output/education_pool.json"):
        self.storage_path = storage_path
        self.pool: List[Dict[str, Any]] = []
        self._load_from_file()

    def _load_from_file(self):
        """Loads existing education points from the JSON file."""
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    self.pool = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                self.pool = []
        else:
            self.pool = []

    def _save_to_file(self):
        """Deduplicates by headline and saves the pool to disk."""
        # Deduplicate: Keep the first occurrence to preserve 'asked' status
        # if the agent tries to re-suggest the same headline.
        dedup_dict = {}
        for item in self.pool:
            headline = item.get("headline")
            if headline not in dedup_dict:
                dedup_dict[headline] = item
        
        self.pool = list(dedup_dict.values())

        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(self.pool, f, indent=4)

    def add_new_points(self, new_points: List[Dict[str, Any]]) -> None:
        """
        Merges new points from the agent into the pool.
        Existing points with the same headline are NOT overwritten 
        to ensure 'asked' status is preserved.
        """
        existing_headlines = {edu["headline"] for edu in self.pool}
        
        for point in new_points:
            if point["headline"] not in existing_headlines:
                # Initialize metadata for new points
                if "status" not in point:
                    point["status"] = "pending"
                self.pool.append(point)
        
        self._save_to_file()

    def pick_and_mark_asked(self) -> Optional[Dict[str, Any]]:
        """
        Picks the highest priority 'pending' education point,
        marks it as 'asked', saves, and returns it.
        Priority: High > Normal > Low.
        """
        # 1. Filter for pending items
        pending = [e for e in self.pool if e.get("status") != "asked"]

        if not pending:
            return None

        # 2. Sort by urgency
        urgency_priority = {"High": 0, "Normal": 1, "Low": 2}
        pending.sort(key=lambda x: urgency_priority.get(x.get("urgency", "Normal"), 1))

        # 3. Select the best candidate
        selected_point = pending[0]
        
        # 4. Update status in the main pool
        for item in self.pool:
            if item["headline"] == selected_point["headline"]:
                item["status"] = "asked"
                break
        
        self._save_to_file()
        return selected_point

    def mark_as_asked(self, headline: str) -> bool:
        """Manually marks a point as asked by its headline."""
        for item in self.pool:
            if item["headline"] == headline:
                item["status"] = "asked"
                self._save_to_file()
                return True
        return False

    def get_pending(self) -> List[Dict[str, Any]]:
        """Returns all points that haven't been shared yet."""
        return [e for e in self.pool if e.get("status") != "asked"]

    def get_all(self) -> List[Dict[str, Any]]:
        """Returns the full pool."""
        return self.pool

    def clear_pool(self):
        """Reset the pool."""
        self.pool = []
        self._save_to_file()