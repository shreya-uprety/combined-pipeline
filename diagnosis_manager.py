from typing import List, Dict, Any
import copy

class DiagnosisManager:
    def __init__(self):
        # Main recursive diagnosis pool
        self.diagnoses = []
        
        # Stores the *previous* returned list to compare against
        self.ranked_temp = [] 

    def _calc_severity(self, points: int, rank_index: int) -> str:
        """Helper to calculate severity based on points and rank position."""
        # 1. HIGH: Must be Rank 1 (index 0) AND have > 8 points
        if (rank_index == 0) and (points > 8):
            return "High"
        # 2. MODERATE: Points > 5
        elif points > 5:
            return "Moderate"
        # 3. LOW: Points > 3
        elif points > 3:
            return "Low"
        # 4. VERY LOW
        else:
            return "Very Low"

    def get_diagnoses(self) -> List[Dict[str, Any]]:
        """Returns the full consolidated list with dynamic re-ranking metrics."""
        
        # 1. Shallow copy to manipulate order
        current_list = copy.copy(self.diagnoses)
        
        if not current_list:
            return []

        # ---------------------------------------------------------
        # DYNAMIC SWAP LOGIC
        # ---------------------------------------------------------
        if len(current_list) >= 2 and len(self.ranked_temp) > 0:
            
            curr_top = current_list[0]
            curr_second = current_list[1]
            prev_top = self.ranked_temp[0]

            # Condition 1: The AI is "stuck" (The top diagnosis hasn't changed from the last cycle)
            if curr_top['did'] == prev_top['did']:
                
                # Calculate severities AS THEY ARE NOW (before swap)
                points_1 = len(curr_top.get('indicators_point', []))
                points_2 = len(curr_second.get('indicators_point', []))
                
                # Note: We calculate severity assuming their current positions (0 and 1)
                sev_1 = self._calc_severity(points_1, 0)
                sev_2 = self._calc_severity(points_2, 1) 

                # -----------------------------------------------------
                # SWAP RULES
                # -----------------------------------------------------
                should_swap = False

                # Rule A: If both are Moderate, they are competitive. Swap to show thinking.
                if (sev_1 == "Moderate") and (sev_2 == "Moderate"):
                    should_swap = True
                
                # Rule B: If both are High (Rare, but possible), swap.
                elif (sev_1 == "High") and (sev_2 == "High"):
                    should_swap = True

                # Rule C (Optional): If Rank 1 is NOT High (i.e., it's weak), allow swapping 
                # to prevent a weak diagnosis from looking like a locked-in answer.
                elif (sev_1 != "High"):
                    should_swap = True

                # EXECUTE SWAP
                if should_swap:
                    current_list[0], current_list[1] = current_list[1], current_list[0]

        # ---------------------------------------------------------
        # FINAL METRICS CALCULATION
        # ---------------------------------------------------------
        ranked_d = []
        for i, d in enumerate(current_list):
            # 1. Update Rank based on the new (potentially swapped) order
            d['rank'] = i + 1
            
            # 2. Recalculate Severity based on new position
            # (e.g., If a "High" dropped to Rank 2, it might become "Moderate" or "Low" if points are low)
            points = len(d.get('indicators_point', [])) 
            d['severity'] = self._calc_severity(points, i)

            ranked_d.append(d)

        # Update history
        self.ranked_temp = ranked_d
        
        return ranked_d

    def get_diagnoses_basic(self) -> List[Dict[str, Any]]:
        """
        Returns the simplified list.
        """
        simplified_list = []
        for item in self.diagnoses:
            simplified_list.append({
                "did": item["did"],
                "headline": item.get("headline"),
                "diagnosis": item["diagnosis"],
                "indicators_point": item["indicators_point"],
                "reasoning": item.get("reasoning"),
            })
        return simplified_list