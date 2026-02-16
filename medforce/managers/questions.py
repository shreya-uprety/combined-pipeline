import uuid
from typing import List, Dict, Optional, Any
import json

class QuestionPoolManager:
    def __init__(self, initial_questions: List[Dict[str, Any]]):
        self.questions = initial_questions

        if initial_questions == []:
            try:
                with open("output/question_pool.json", "r") as file:
                    self.questions = json.load(file)
            except (FileNotFoundError, json.JSONDecodeError):
                self.questions = []

        # Deduplicate and save immediately upon initialization
        self._save_to_file()

    def _save_to_file(self):
        """
        Deduplicates questions by QID (keeping the latest version)
        and writes the cleaned list to question_pool.json.
        """
        # 1. Deduplicate: Using a dictionary comprehension where QID is the key.
        dedup_dict = {q["qid"]: q for q in self.questions}

        # 2. Update the in-memory list to match the deduplicated state
        self.questions = list(dedup_dict.values())

        # 3. Save to disk
        with open("output/question_pool.json", "w", encoding="utf-8") as file:
            json.dump(self.questions, file, indent=4)

    def delete_by_content(self, content: str) -> bool:
        """
        Deletes question(s) matching the provided content string.
        Comparison is case-insensitive and ignores leading/trailing whitespace.
        Returns True if at least one item was deleted, False otherwise.
        """
        if not content or not isinstance(content, str):
            return False

        # Normalize the target string for comparison
        target_normalized = content.strip().lower()
        original_count = len(self.questions)

        # Rebuild the list, keeping only items that DO NOT match the target
        self.questions = [
            q for q in self.questions
            if q.get("content", "").strip().lower() != target_normalized
        ]

        # If the list size changed, we successfully deleted something
        if len(self.questions) < original_count:
            self._save_to_file()
            return True

        return False

    def update_pool(self):
        with open("output/question_pool.json", "r") as file:
            self.questions = json.load(file)

    def add_from_strings(self, questions: List[str]) -> None:
        """
        Adds a list of question strings to the pool.
        Generates a random QID, sets status/answer to None.
        Does NOT assign a rank.
        PREVENTS DUPLICATES: Checks if question content already exists.
        """
        # Create a set of existing question contents (normalized) for fast lookup
        existing_contents = {
            q.get("content", "").strip().lower()
            for q in self.questions
            if q.get("content")
        }

        for q_text in questions:
            # Validate input
            if not q_text or not isinstance(q_text, str) or not q_text.strip():
                continue

            clean_text = q_text.strip()
            normalized_text = clean_text.lower()

            # Check if this question already exists
            if normalized_text in existing_contents:
                continue

            new_q = {
                "qid": str(uuid.uuid4()),
                "content": clean_text,
                "status": None,
                "answer": None
            }

            self.questions.append(new_q)

            # Add to the checking set immediately
            # (prevents duplicates within the input list itself)
            existing_contents.add(normalized_text)

        self._save_to_file()


    def add_questions(self, text_list: List[Dict[str, str]]) -> None:
        """
        Reranks all 'None' status questions and adds new ones.
        """
        existing_map = {q["qid"]: q for q in self.questions}
        new_priority_ids = [q.get('qid') for q in text_list]
        updated_unasked = []

        # Add/Update prioritized questions
        for i, q_data in enumerate(text_list):
            qid = q_data.get('qid')
            if qid in existing_map:
                existing_map[qid]["content"] = q_data.get('question')
                existing_map[qid]["rank"] = i + 1
                updated_unasked.append(existing_map[qid])
            else:
                new_q = {
                    "qid": qid,
                    "content": q_data.get('question'),
                    "status": None,
                    "answer": None,
                    "rank": i + 1
                }
                self.questions.append(new_q)
                updated_unasked.append(new_q)

        # Rerank existing unasked questions that were not in the new list
        others_to_rerank = [
            q for q in self.questions
            if q["status"] is None and q["qid"] not in new_priority_ids
        ]

        others_to_rerank.sort(key=lambda x: x.get("rank", 998))

        current_rank = len(updated_unasked) + 1
        for q in others_to_rerank:
            q["rank"] = current_rank
            current_rank += 1

        # This call now handles the deduplication and saving
        self._save_to_file()

    def get_high_rank_question(self, target_rank: Optional[int] = None) -> Optional[Dict]:
        # Filter for questions where status is None
        candidates = [q for q in self.questions if q["status"] is None]

        if not candidates:
            return None

        # Option 1: If a target rank is specified, find the first match
        if target_rank is not None:
            for q in candidates:
                if q["rank"] == target_rank:
                    return q
            return min(candidates, key=lambda x: x["rank"])

        # Option 2: Default behavior - return the one with the lowest rank number
        return min(candidates, key=lambda x: x["rank"])

    def get_questions_basic(self):
        return [
            {"qid": q["qid"], "question": q["content"]}
            for q in self.questions if q["status"] is None
        ]

    def get_questions(self) -> List[Dict]:
        return self.questions

    def get_unanswered_questions(self) -> List[Dict[str, Any]]:
        """
        Returns all question objects where the answer is None or an empty string.
        """
        return [
            q for q in self.questions
            if q.get("answer") is None or (isinstance(q.get("answer"), str) and q.get("answer").strip() == "")
        ]

    def update_status(self, qid: str, new_status: str) -> bool:
        for q in self.questions:
            if q["qid"] == qid:
                q["status"] = new_status
                q["rank"] = 999
                self._save_to_file()
                return True
        return False

    def update_answer(self, qid: str, answer: str) -> bool:
        for q in self.questions:
            if q["qid"] == qid:
                q["answer"] = answer
                q["rank"] = 999
                self._save_to_file()
                return True
        return False

    def update_enriched_questions(self, enriched_list: List[Dict[str, Any]]) -> None:
        """
        Updates existing questions in the pool with enriched metadata (headline, domain, etc.).
        Matches based on QID.
        """
        # Create a lookup map for the current pool for efficiency
        pool_map = {q["qid"]: q for q in self.questions}

        for enriched_item in enriched_list:
            qid = enriched_item.get("qid")
            if qid in pool_map:
                pool_map[qid].update(enriched_item)

        # Persist the enriched data to question_pool.json
        self._save_to_file()
