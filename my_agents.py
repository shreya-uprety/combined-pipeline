import os
import json
import base64
import uuid
import asyncio
import logging
from google import genai
from google.genai import types
from fastapi import WebSocket
from PIL import Image
from io import BytesIO
import bucket_ops

from dotenv import load_dotenv
load_dotenv()
# Configure logging
logger = logging.getLogger("medforce-backend")


MODEL = "gemini-2.5-flash-lite"
MODEL_PRE_CONSULT = "gemini-3-flash-preview"
IMAGE_MODEL = "gemini-3-pro-image-preview"
IMAGE_MODEL2 = "gemini-2.5-flash-image"

class BaseLogicAgent:
    def __init__(self):
        self._client = None  # Lazy initialization
    
    @property
    def client(self):
        """Lazy initialization of genai client"""
        if self._client is None:
            self._client = genai.Client(
                api_key=os.getenv("GOOGLE_API_KEY")
            )
        return self._client



class PatientManager(BaseLogicAgent):
    def __init__(self, args: dict = None):
        super().__init__()
        # Additional initialization if needed
        self.args = args
        if  self.args.get("patient_id"):
            os.makedirs(f"output/{self.args['patient_id']}", exist_ok=True)
            self.output_dir = f"output/{self.args['patient_id']}"

        self.bucket_path = f"patient_data/{self.args.get('patient_id')}"

        self.gcs = bucket_ops.GCSBucketManager(bucket_name="clinic_sim")

        self.patient_profile = ""
        self.patient_system_prompt = ""
        self.encounters = []
        self.labs = []



    async def generate_patient_profile(self, input_criteria = None):
        if not input_criteria: 
            input_criteria = self.args
        
        with open("system_prompts/patient_generator.md", "r", encoding="utf-8") as f: 
            system_instruction = f.read()

        prompt_content = f"Please generate a patient profile based on these parameters:\n{json.dumps(input_criteria, indent=2)}"

        response = await self.client.aio.models.generate_content(
            model=MODEL, 
            contents=prompt_content,
            config=types.GenerateContentConfig(
                response_mime_type="text/plain", # Returns raw text/markdown
                system_instruction=system_instruction, 
                temperature=0.7 # Higher temperature for better storytelling/narrative
            )
        )
        
        # Return the generated text directly
        self.patient_profile = response.text
        with open(f"{self.output_dir}/patient_profile.txt", "w", encoding="utf-8") as f:
            f.write(self.patient_profile)


        await self.generate_basic_info(self.patient_profile)
            
        return response.text
    
    async def generate_basic_info(self, patient_profile_text):
        if not patient_profile_text: 
            patient_profile_text = self.patient_profile
        
        with open("system_prompts/basic_info_extractor.md", "r", encoding="utf-8") as f: 
            system_instruction = f.read()
        with open("response_schema/basic_info.json", "r", encoding="utf-8") as f:
            response_schema = json.load(f)

        try:
            prompt_content = f"PATIENT PROFILE:\n{patient_profile_text}\n{json.dumps(self.args)}\nTASK: Extract the basic demographic and administrative info for this patient."

            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.3 # Low temp for factual extraction
                )
            )
            
            res = json.loads(response.text)
            self.gcs.create_file_from_string(json.dumps(res), f"{self.bucket_path}/basic_info.json", content_type="application/json")
            return res
            
        except Exception as e:
            print(f"Error in generate_basic_info: {e}") 
            return {}
    
    async def generate_system_prompt(self, patient_profile_text):
        if not patient_profile_text: 
            patient_profile_text = self.patient_profile


        with open("system_prompts/system_prompt_generator.md", "r", encoding="utf-8") as f: 
            system_instruction = f.read()

        prompt_content = (
            f"SOURCE PATIENT PROFILE:\n"
            f"{patient_profile_text}\n\n"
            f"TASK:\n"
            f"Write the SYSTEM PROMPT for this patient."
        )

        response = await self.client.aio.models.generate_content(
            model=MODEL, 
            contents=prompt_content,
            config=types.GenerateContentConfig(
                response_mime_type="text/plain", 
                system_instruction=system_instruction, 
                temperature=0.5 # Lower temp to stick strictly to the facts provided in the profile
            )
        )
        self.patient_system_prompt = response.text
        with open(f"{self.output_dir}/system_prompt.txt", "w", encoding="utf-8") as f:
            f.write(self.patient_system_prompt)
        return response.text

    async def generate_encounters_narrative(self, patient_profile_text, criteria):

        with open("system_prompts/encounter_narrative.md", "r", encoding="utf-8") as f: 
            system_instruction = f.read()

        if not patient_profile_text: 
            return "Error: No patient profile provided."
        
        try:
            # We combine the profile with the specific instructions for this run
            prompt_content = (
                f"### PATIENT MASTER PROFILE ###\n"
                f"{patient_profile_text}\n\n"
                f"### ENCOUNTER GENERATION CRITERIA ###\n"
                f"{json.dumps(criteria, indent=2)}\n\n"
                f"### TASK ###\n"
                f"Generate the detailed clinical narrative for these encounters. "
                f"Ensure the timeline makes sense (dates relative to Today). "
                f"For each encounter, provide full SOAP notes (Subjective, Objective, Assessment, Plan)."
            )

            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="text/plain", 
                    system_instruction=system_instruction, 
                    temperature=0.5 # Balanced creativity and logic
                )
            )
            
            return response.text
            
        except Exception as e:
            print(f"Error in generate_encounter_story: {e}") 
            return f"Error: {str(e)}"

    async def generate_referral_letter(self):
        with open("system_prompts/referral_generator.md", "r", encoding="utf-8") as f: 
                system_instruction = f.read()

        patient_profile_text = self.gcs.read_file_as_string(f"patient_data/{self.args.get('patient_id')}/patient_profile.txt")
        encounter_narrative_text = self.gcs.read_file_as_string(f"patient_data/{self.args.get('patient_id')}/encounter_narrative.txt")
        try:
            # We explicitly instruct the model to look at the LAST encounter
            prompt_content = (
                f"### PATIENT PROFILE ###\n"
                f"{patient_profile_text}\n\n"
                f"### ENCOUNTER HISTORY (Chronological) ###\n"
                f"{encounter_narrative_text}\n\n"
                f"### TASK ###\n"
                f"Write a formal Medical Referral Letter based on the **MOST RECENT** encounter in the history above. "
                f"Address it to the appropriate Specialist (e.g., Hepatologist, Cardiologist) based on the diagnosis. "
                f"Include the header, date, patient details, clinical summary, and signature. "
                f"Format it strictly as a physical document text."
            )

            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="text/plain", 
                    system_instruction=system_instruction, 
                    temperature=0.4 # Professional and consistent
                )
            )

            referal_letter_text = response.text

            img_op_path = await self.generate_referral_img(referal_letter_text, output_filename=f"{self.output_dir}/referral_letter.png")
            
            self.gcs.create_file_from_string(referal_letter_text, f"{self.bucket_path}/raw_data/referral_letter.txt", content_type="text/plain")
            self.gcs.upload_file(img_op_path, f"{self.bucket_path}/raw_data/referral_letter.png")
            return response.text
            
        except Exception as e:
            print(f"Error in generate_letter_text: {e}") 
            return "Error generating letter."

    async def generate_referral_img(self, letter_text, output_filename="referral_letter.png"):
        """
        Generates a photo of the printed letter.
        """
        if not letter_text:
            print("Error: No letter text provided.")
            return None

        # Construct a prompt that describes the physical object
        prompt = (
            f"A realistic, high-resolution, top-down close-up photo of a printed Medical Referral Letter. "
            f"The paper is white, clean, A4 size, folded slightly. "
            f"It has a formal clinic letterhead at the top. "
            f"The visible text on the paper corresponds to:\n\n"
            f"{letter_text}\n\n"
            f"Ensure the 'Re: Patient Name' and the 'Reason for Referral' are legible. "
            f"Include a handwritten blue ink signature at the bottom."
        )

        models_to_try = [IMAGE_MODEL, IMAGE_MODEL2]

        for model_name in models_to_try:
            try:
                print(f"Generating image for referral letter using {model_name}...")
                
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        image_config=types.ImageConfig(
                            aspect_ratio="9:16", # Letter aspect ratio
                        ),
                    )
                )

                for part in response.parts:
                    if part.inline_data is not None:
                        image = part.as_image()
                        image.save(output_filename)
                        print(f"Success: Referral Image saved to {output_filename}")
                        return output_filename
                    elif part.text is not None:
                        print(f"Warning: {model_name} returned text: {part.text}")

            except Exception as e:
                print(f"Error with {model_name}: {e}")
                continue

        return None

    async def generate_encounters(self, patient_profile_text):

        if not patient_profile_text: 
            patient_profile_text = self.patient_profile
        
        with open("system_prompts/encounter_generator.md", "r", encoding="utf-8") as f: 
            system_instruction = f.read()
        with open("response_schema/encounter.json", "r", encoding="utf-8") as f:
            response_schema = json.load(f)

        encounter_narrative = await self.generate_encounters_narrative(patient_profile_text, self.args)

        self.gcs.create_file_from_string(encounter_narrative, f"{self.bucket_path}/encounter_narrative.txt", content_type="text/plain")

        try:
            # We explicitly ask for a list of encounters based on the profile
            prompt = f"Patient Profile:\n{patient_profile_text}\n\nEncounter Narrative:\n{encounter_narrative}\nTask: Generate the past medical encounters timeline for this patient as a JSON array."
            
            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_schema=response_schema, 
                    response_mime_type="application/json", 
                    system_instruction=system_instruction,
                    temperature=0.4 # Keep history consistent and logical, less random
                )
            )
            
            res = json.loads(response.text)
            self.encounters = res
            with open(f"{self.output_dir}/encounters.json", "w", encoding="utf-8") as f:
                json.dump(self.encounters, f, indent=4)
            return res
        except Exception as e:
            print(f"Error in generate_encounters: {e}") 
            return []

    async def generate_labs(self, patient_profile_text, encounters):
        if not patient_profile_text and not encounters: 
            patient_profile_text = self.patient_profile
            encounters = self.encounters

        with open("system_prompts/lab_generator.md", "r", encoding="utf-8") as f: 
            system_instruction = f.read()
        with open("response_schema/labs.json", "r", encoding="utf-8") as f:
            response_schema = json.load(f)

        try:
            # We prompt the model to focus on the 'Clinical Reasoning' section of the profile
            prompt = (
                f"PATIENT PROFILE:\n{patient_profile_text}\n\n"
                f"PATIENT ENCOUNTRES:\n{json.dumps(encounters)}\n\n"
                f"TASK: Generate a full lab report (CBC and Chemistry) for this patient. "
                f"Ensure the abnormal values align with the diagnosis described above."
            )
            
            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.3 # Low temperature is crucial for math/range consistency
                )
            )
            
            res = json.loads(response.text)

            self.labs = res
            with open(f"{self.output_dir}/labs.json", "w", encoding="utf-8") as f:
                json.dump(self.labs, f, indent=4)
            return res
        except Exception as e:
            print(f"Error in generate_labs: {e}") 
            return []

    def group_labs_by_date(self,labs_data):

        grouped = {}

        for item in labs_data:
            # Extract static metadata for this biomarker
            biomarker_name = item.get("biomarker")
            unit = item.get("unit")
            ref_min = item.get("referenceRange", {}).get("min")
            ref_max = item.get("referenceRange", {}).get("max")

            # Iterate through the time-series values
            for measurement in item.get("values", []):
                timestamp = measurement.get("t")
                value = measurement.get("value")

                # Initialize the date group if it doesn't exist
                if timestamp not in grouped:
                    grouped[timestamp] = {
                        "date_time": timestamp,
                        "labs": []
                    }

                # Determine Flag (High/Low/Normal)
                flag = "NORMAL"
                if ref_min is not None and ref_max is not None:
                    if value < ref_min:
                        flag = "LOW"
                    elif value > ref_max:
                        flag = "HIGH"

                # Add this specific result to the group
                grouped[timestamp]["labs"].append({
                    "biomarker": biomarker_name,
                    "value": value,
                    "unit": unit,
                    "reference_range": f"{ref_min} - {ref_max}",
                    "flag": flag
                })

        # Convert dictionary to a list and sort by date (Newest last)
        # ISO 8601 dates (YYYY-MM-DD) sort correctly as strings
        sorted_results = sorted(list(grouped.values()), key=lambda x: x['date_time'])
        
        return sorted_results

    async def imaging_doc_parser(self, encounter_object):
        with open("system_prompts/imaging_report_generator.md", "r", encoding="utf-8") as f: 
                system_instruction = f.read()

        if not encounter_object: 
            return None
        
        try:
            # Extract key context to help the LLM write an accurate report
            enc = encounter_object.get('encounter', encounter_object)
            pat = encounter_object.get('patient', {})
            
            # Try to find what imaging was ordered
            plan_investigations = enc.get('plan', {}).get('investigations', {})
            # Fallback logic if specific imaging key isn't found
            ordered_test = str(plan_investigations) if plan_investigations else "Chest X-Ray (Assumed)"
            
            diagnosis = enc.get('assessment', {}).get('impression', 'Under Investigation')
            symptoms = enc.get('chief_complaint', 'N/A')
            
            context_summary = {
                "patient": pat,
                "ordered_exam": ordered_test,
                "diagnosis_to_support": diagnosis,
                "indication": symptoms
            }

            prompt_content = (
                f"### CLINICAL CONTEXT ###\n"
                f"{json.dumps(context_summary, indent=2)}\n\n"
                f"### TASK ###\n"
                f"Write the full text of the Radiology Report for the exam listed above. "
                f"The 'Findings' must support the diagnosis of '{diagnosis}'. "
                f"If the diagnosis is serious, the findings must be abnormal. "
                f"Format it as a clean, professional medical document."
            )

            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="text/plain", 
                    system_instruction=system_instruction, 
                    temperature=0.4 # Low temp for professional medical tone
                )
            )
            
            return response.text
            
        except Exception as e:
            print(f"Error in generate_report_content: {e}") 
            return "Error generating imaging report."


    async def lab_doc_parser(self, lab_encounter_json,patient_name):
        with open("system_prompts/lab_parser.md", "r", encoding="utf-8") as f: 
            system_instruction = f.read()

        if not lab_encounter_json: 
            return "Error: Empty Data"
        
        try:
            # We add the patient name to the prompt context so the header is complete
            input_context = {
                "patient_name": patient_name,
                "lab_data": lab_encounter_json
            }
            
            prompt_content = (
                f"RAW LAB DATA:\n{json.dumps(input_context, indent=2)}\n\n"
                f"TASK: Format this into a clean, fixed-width Laboratory Result Report."
            )

            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="text/plain", 
                    system_instruction=system_instruction, 
                    temperature=0.1 # Very low temperature to ensure numbers are copied exactly
                )
            )
            
            return response.text
            
        except Exception as e:
            print(f"Error in generate_lab_content: {e}") 
            return "Error parsing lab document."


    async def encounter_doc_parser(self, encounter_object):
        with open("system_prompts/encounter_parser.md", "r", encoding="utf-8") as f: 
            system_instruction = f.read()

        if not encounter_object: 
            return "Error: Empty Data"
        
        try:
            # We pass the raw JSON to the model
            prompt_content = f"Raw Encounter Data:\n{json.dumps(encounter_object, indent=2)}\n\nTask: Format this into a printable Medical Summary Report text."

            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="text/plain", # We want formatted text, not JSON
                    system_instruction=system_instruction, 
                    temperature=0.3 # Low temperature for consistent formatting
                )
            )
            
            return response.text
            
        except Exception as e:
            print(f"Error in generate_document_content: {e}") 
            return "Error parsing document."

    async def generate_encounter_img(self, document_text, output_filename="encounter_report.png"):

        if not document_text:
            print("Error: No document text provided.")
            return None
        


        # Construct a prompt that asks the model to render the specific text
        # Note: Generative models may summarize text visually; they are not PDF printers.
        # We emphasize "legible text" to get the best result.
        prompt = (
            f"A realistic, high-resolution, top-down close-up photo of a printed medical summary report "
            f"The paper is white and clean. Potrait A4 size. "
            f"The document is clearly formatted with headers. "
            f"The visible text content on the page corresponds to:\n\n"
            f"{document_text}\n\n"
            f"Ensure the Hospital Header and Patient Name are prominent and legible. Generate the fictional signature of the doctor at the bottom."
        )

        models_to_try = [IMAGE_MODEL, IMAGE_MODEL2]

        for model_name in models_to_try:
            try:
                print(f"Generating image for radiology report using {model_name}...")
                
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        image_config=types.ImageConfig(
                            aspect_ratio="9:16",
                        ),
                    )
                )

                # Check response parts for image data
                for part in response.parts:
                    if part.inline_data is not None:
                        # SUCCESS: Save and return immediately
                        image = part.as_image()
                        image.save(output_filename)
                        print(f"Success: Image generated with {model_name} and saved to {output_filename}")
                        return output_filename
                    elif part.text is not None:
                        # Warning: Model returned text (likely a refusal or safety filter)
                        print(f"Warning: {model_name} returned text instead of image: {part.text}")

                print(f"{model_name} failed to produce an image. Retrying with next model...")

            except Exception as e:
                print(f"Error encountered with {model_name}: {e}. Retrying with next model...")
                continue # Explicitly continue to the next model in the list

        # If loop finishes and no image was returned
        print("Error: All image generation models failed.")
        return None


    async def generate_lab_img(self, lab_object, patient_name, output_filename="lab_report.png"):

        if not lab_object:
            print("Error: No lab object provided.")
            return None
        
        # Generate the formatted text table first
        document_text = await self.lab_doc_parser(lab_object, patient_name)

        # Construct a prompt specifically for tabular lab data
        prompt = (
            f"A realistic, high-resolution, top-down close-up photo of a printed Laboratory Result Report. "
            f"The paper is white and clean. Portrait A4 size. "
            f"The document features a clearly structured text-based table. "
            f"The visible text content on the page corresponds to:\n\n"
            f"{document_text}\n\n"
            f"Ensure the columns (Test Name, Result, Flag) are visually aligned like a real monospaced medical printout. "
            f"The 'HIGH' or 'LOW' flags should be clearly distinct. "
            f"Include a 'Verified by Pathologist' signature or stamp at the bottom."
        )

        models_to_try = [IMAGE_MODEL, IMAGE_MODEL2]

        for model_name in models_to_try:
            try:
                print(f"Generating image for lab report using {model_name}...")
                
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        image_config=types.ImageConfig(
                            aspect_ratio="9:16",
                        ),
                    )
                )

                # Check response parts for image data
                for part in response.parts:
                    if part.inline_data is not None:
                        # SUCCESS: Save and return immediately
                        image = part.as_image()
                        image.save(output_filename)
                        print(f"Success: Image generated with {model_name} and saved to {output_filename}")
                        return output_filename
                    elif part.text is not None:
                        # Warning: Model returned text (likely a refusal or safety filter)
                        print(f"Warning: {model_name} returned text instead of image: {part.text}")

                print(f"{model_name} failed to produce an image. Retrying with next model...")

            except Exception as e:
                print(f"Error encountered with {model_name}: {e}. Retrying with next model...")
                continue # Explicitly continue to the next model in the list

        # If loop finishes and no image was returned
        print("Error: All image generation models failed.")
        return None

    async def generate_imaging_report_img(self, imaging_doc_text, output_filename="radiology_report.png"):

        if not imaging_doc_text:
            print("Error: No imaging document text provided.")
            return None

        with open(f"{self.output_dir}/imaging_doc_text.txt", "w", encoding="utf-8") as f:
            f.write(imaging_doc_text)

        # CLEANING STEP: 
        # The previous agent adds a hidden "[[IMAGE_PROMPT...]]" at the bottom.
        # We must strip that out because we don't want it printed on the fake paper.
        clean_document_text = imaging_doc_text

        # if "[[" in imaging_doc_text:
        #     clean_document_text = imaging_doc_text.split("[[")[0].strip()
        # else:
        #     clean_document_text = imaging_doc_text

        # Construct a prompt specifically for a text-heavy radiology document
        prompt = (
            f"A realistic, high-resolution, top-down close-up photo of a printed Radiology/Imaging Report. "
            f"The paper is white and clean. Portrait A4 size. "
            f"The document features a formal header 'DEPARTMENT OF RADIOLOGY'. "
            f"The visible text content on the page corresponds to:\n\n"
            f"{clean_document_text}\n\n"
            f"Ensure the text is legible, appearing like a standard laser-printed medical record. "
            f"The 'FINDINGS' and 'IMPRESSION' sections should be clearly distinct. "
            f"Include a signature at the bottom."
        )

        models_to_try = [IMAGE_MODEL, IMAGE_MODEL2]

        for model_name in models_to_try:
            try:
                print(f"Generating image for radiology report using {model_name}...")
                
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        image_config=types.ImageConfig(
                            aspect_ratio="9:16",
                        ),
                    )
                )

                # Check response parts for image data
                for part in response.parts:
                    if part.inline_data is not None:
                        # SUCCESS: Save and return immediately
                        image = part.as_image()
                        image.save(output_filename)
                        print(f"Success: Image generated with {model_name} and saved to {output_filename}")
                        return output_filename
                    elif part.text is not None:
                        # Warning: Model returned text (likely a refusal or safety filter)
                        print(f"Warning: {model_name} returned text instead of image: {part.text}")

                print(f"{model_name} failed to produce an image. Retrying with next model...")

            except Exception as e:
                print(f"Error encountered with {model_name}: {e}. Retrying with next model...")
                continue # Explicitly continue to the next model in the list

        # If loop finishes and no image was returned
        print("Error: All image generation models failed.")
        return None


    async def generate_pre_consultation_chat(self):
        with open("system_prompts/pre_consult_chat_generator.md", "r", encoding="utf-8") as f: 
            system_instruction = f.read()
        with open("response_schema/pre_consult_chat_generator.json", "r", encoding="utf-8") as f:
            response_schema = json.load(f)

        patient_profile = self.gcs.read_file_as_string(f"{self.bucket_path}/patient_profile.txt")
        file_inventory = json.loads(self.gcs.read_file_as_string(f"{self.bucket_path}/raw_data.json"))
        try:
            # 1. Summarize the Context for the LLM
            # We explicitly list the filenames so the LLM knows what to "upload"
            context = {
                "available_files": file_inventory,
                "appointment_context": "Specialist Consultation Booking",
                "clinic_name": "General Hepatology Clinic"
            }

            prompt_content = (
                f"### PATIENT PROFILE (PERSONA) ###\n{patient_profile}\n\n"
                f"### MEDICAL HISTORY SUMMARY ###\n(Refer to the current encounter in the profile for symptoms)\n\n"
                f"### FILE INVENTORY (Documents the patient possesses) ###\n{json.dumps(context, indent=2)}\n\n"
                f"### TASK ###\n"
                f"Generate a WhatsApp-style chat transcript between 'admin' (Nurse/Receptionist) and 'patient'.\n"
                f"The Admin must verify identity, ask about symptoms, and request the documents listed in the Inventory.\n"
                f"The Patient must answer according to their Persona (e.g., if anxious, sound anxious) and upload the files when asked."
            )

            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.6 # Moderate temp for natural conversation flow
                )
            )
            
            res = json.loads(response.text)
            self.gcs.create_file_from_string(json.dumps(res, indent=4), f"{self.bucket_path}/pre_consultation_chat.json", content_type="application/json")
            return res
        except Exception as e:
            print(f"Error in generate_transcript: {e}") 
            return {"conversation": []}
        

    async def generate_ground_truth(self):
        print("Generating Ground Truth Data...")
        print("Generating Patient Profile...")
        patient_profile = await self.generate_patient_profile()
        self.gcs.create_file_from_string(patient_profile, f"{self.bucket_path}/patient_profile.txt", content_type="text/plain")

        print("Generating System Prompt...")
        patient_system_prompt = await self.generate_system_prompt(patient_profile)
        self.gcs.create_file_from_string(patient_system_prompt, f"{self.bucket_path}/system_prompt.txt", content_type="text/plain")

        print("Generating Encounters...")
        encounters = await self.generate_encounters(patient_profile)
        self.gcs.create_file_from_string(json.dumps(encounters, indent=4), f"{self.bucket_path}/encounters.json", content_type="application/json")

        print("Generating Labs...")
        labs = await self.generate_labs(patient_profile, encounters)
        self.gcs.create_file_from_string(json.dumps(labs, indent=4), f"{self.bucket_path}/labs.json", content_type="application/json")

        grouped_labs = self.group_labs_by_date(labs)

        encounter_docs = []
        for i, encounter in enumerate(encounters):
            encounter_doc = await self.encounter_doc_parser(encounter)
            encounter_docs.append({
                "file" : f"encounter_report_{i}_{encounter['encounter']['meta']['date_time'].split('T')[0]}.txt",
                "date_time": encounter['encounter']['meta']['date_time'],
                "encounter_report_text": encounter_doc
            })
            # Save each individual encounter report text file
            self.gcs.create_file_from_string(
                encounter_doc, 
                f"{self.bucket_path}/raw_data/encounter_report_{i}_{encounter['encounter']['meta']['date_time'].split('T')[0]}.txt", 
                content_type="text/plain"
            )


        lab_docs = []
        for i, lab_entry in enumerate(grouped_labs):
            lab_doc = await self.lab_doc_parser(lab_entry, encounters[0].get("patient",{}).get("name"))
            lab_docs.append({
                "file" : f"lab_report_{i}_{lab_entry['date_time'].split('T')[0]}.txt",
                "image_file" : f"lab_report_{i}_{lab_entry['date_time'].split('T')[0]}.txt",
                "date_time": lab_entry["date_time"],
                "lab_report_text": lab_doc
            })
            # Save each individual lab report text file
            self.gcs.create_file_from_string(
                lab_doc, 
                f"{self.bucket_path}/raw_data/lab_report_{i}_{lab_entry['date_time'].split('T')[0]}.txt", 
                content_type="text/plain"
            )

        imaging_docs = []
        for i, encounter in enumerate(encounters):
            if encounter.get("encounter",{}).get("plan",{}).get("investigations",{}).get("imaging"):
                imaging_doc = await self.imaging_doc_parser(encounter)
                imaging_docs.append({
                    "file" : f"imaging_report_{i}_{encounter['encounter']['meta']['date_time'].split('T')[0]}.txt",
                    "date_time": encounter['encounter']['meta']['date_time'],
                    "imaging_report_text": imaging_doc
                })
                # Save each individual imaging report text file
                self.gcs.create_file_from_string(
                    imaging_doc, 
                    f"{self.bucket_path}/raw_data/imaging_report_{i}_{encounter['encounter']['meta']['date_time'].split('T')[0]}.txt", 
                    content_type="text/plain"
                )

        raw_data = {
            "encounter_reports": encounter_docs,
            "lab_reports": lab_docs,
            "imaging_reports": imaging_docs
        }
        self.gcs.create_file_from_string(json.dumps(raw_data, indent=4), f"{self.bucket_path}/raw_data.json", content_type="application/json")

        ### GENERATE IMAGES
        print("Generating Encounter Images...")
        for enc_doc in encounter_docs:
            img_file = await self.generate_encounter_img(enc_doc["encounter_report_text"], output_filename=f"{self.output_dir}/{enc_doc['file'].replace('.txt','.png')}")
            if img_file:
                # Upload to GCS
                with open(img_file, "rb") as f:
                    self.gcs.create_file_from_string(
                        f.read(), 
                        f"{self.bucket_path}/raw_data/{enc_doc['file'].replace('.txt','.png')}", 
                        content_type="image/png"
                    )
        print("Generating Lab Images...")
        for lab_doc in lab_docs:
            img_file = await self.generate_lab_img(
                lab_entry, 
                encounters[0].get("patient",{}).get("name"), 
                output_filename=f"{self.output_dir}/{lab_doc['file'].replace('.txt','.png')}"
            )
            if img_file:
                # Upload to GCS
                with open(img_file, "rb") as f:
                    self.gcs.create_file_from_string(
                        f.read(), 
                        f"{self.bucket_path}/raw_data/{lab_doc['file'].replace('.txt','.png')}", 
                        content_type="image/png"
                    )
        print("Generating Imaging Report Images...")
        for img_doc in imaging_docs:
            img_file = await self.generate_imaging_report_img(
                img_doc["imaging_report_text"], 
                output_filename=f"{self.output_dir}/{img_doc['file'].replace('.txt','.png')}"
            )
            if img_file:
                # Upload to GCS
                with open(img_file, "rb") as f:
                    self.gcs.create_file_from_string(
                        f.read(), 
                        f"{self.bucket_path}/raw_data/{img_doc['file'].replace('.txt','.png')}", 
                        content_type="image/png"
                    )

        print()
        res = {
            "conversation" : [
                    {
                    'sender': 'admin',
                    'message': 'Hello, this is Linda the Hepatology Clinic admin desk. How can I help you today?'
                    }
            ]
        }
        self.gcs.create_file_from_string(json.dumps(res, indent=4), f"patient_data/{self.args.get('patient_id')}/pre_consultation_chat.json", content_type="application/json")

    async def generate_ground_truth_patient(self):
        print("Generating Ground Truth Data...")
        print("Generating Patient Profile...")
        patient_profile = await self.generate_patient_profile()
        self.gcs.create_file_from_string(patient_profile, f"{self.bucket_path}/patient_profile.txt", content_type="text/plain")

        print("Generating System Prompt...")
        patient_system_prompt = await self.generate_system_prompt(patient_profile)
        self.gcs.create_file_from_string(patient_system_prompt, f"{self.bucket_path}/system_prompt.txt", content_type="text/plain")

        print("Generating Encounters...")
        encounters = await self.generate_encounters(patient_profile)
        self.gcs.create_file_from_string(json.dumps(encounters, indent=4), f"{self.bucket_path}/encounters.json", content_type="application/json")

        print("Generating Labs...")
        labs = await self.generate_labs(patient_profile, encounters)
        self.gcs.create_file_from_string(json.dumps(labs, indent=4), f"{self.bucket_path}/labs.json", content_type="application/json")

        grouped_labs = self.group_labs_by_date(labs)

        encounter_docs = []
        for i, encounter in enumerate(encounters):
            encounter_doc = await self.encounter_doc_parser(encounter)
            encounter_docs.append({
                "file" : f"encounter_report_{i}_{encounter['encounter']['meta']['date_time'].split('T')[0]}.txt",
                "date_time": encounter['encounter']['meta']['date_time'],
                "encounter_report_text": encounter_doc
            })
            # Save each individual encounter report text file
            self.gcs.create_file_from_string(
                encounter_doc, 
                f"{self.bucket_path}/raw_data_generated/encounter_report_{i}_{encounter['encounter']['meta']['date_time'].split('T')[0]}.txt", 
                content_type="text/plain"
            )


        lab_docs = []
        for i, lab_entry in enumerate(grouped_labs):
            lab_doc = await self.lab_doc_parser(lab_entry, encounters[0].get("patient",{}).get("name"))
            lab_docs.append({
                "file" : f"lab_report_{i}_{lab_entry['date_time'].split('T')[0]}.txt",
                "image_file" : f"lab_report_{i}_{lab_entry['date_time'].split('T')[0]}.txt",
                "date_time": lab_entry["date_time"],
                "lab_report_text": lab_doc
            })
            # Save each individual lab report text file
            self.gcs.create_file_from_string(
                lab_doc, 
                f"{self.bucket_path}/raw_data_generated/lab_report_{i}_{lab_entry['date_time'].split('T')[0]}.txt", 
                content_type="text/plain"
            )

        imaging_docs = []
        for i, encounter in enumerate(encounters):
            if encounter.get("encounter",{}).get("plan",{}).get("investigations",{}).get("imaging"):
                imaging_doc = await self.imaging_doc_parser(encounter)
                imaging_docs.append({
                    "file" : f"imaging_report_{i}_{encounter['encounter']['meta']['date_time'].split('T')[0]}.txt",
                    "date_time": encounter['encounter']['meta']['date_time'],
                    "imaging_report_text": imaging_doc
                })
                # Save each individual imaging report text file
                self.gcs.create_file_from_string(
                    imaging_doc, 
                    f"{self.bucket_path}/raw_data_generated/imaging_report_{i}_{encounter['encounter']['meta']['date_time'].split('T')[0]}.txt", 
                    content_type="text/plain"
                )

        raw_data = {
            "encounter_reports": encounter_docs,
            "lab_reports": lab_docs,
            "imaging_reports": imaging_docs
        }
        self.gcs.create_file_from_string(json.dumps(raw_data, indent=4), f"{self.bucket_path}/raw_data.json", content_type="application/json")

        ### GENERATE IMAGES
        print("Generating Encounter Images...")
        for enc_doc in encounter_docs:
            img_file = await self.generate_encounter_img(enc_doc["encounter_report_text"], output_filename=f"{self.output_dir}/{enc_doc['file'].replace('.txt','.png')}")
            if img_file:
                # Upload to GCS
                with open(img_file, "rb") as f:
                    self.gcs.create_file_from_string(
                        f.read(), 
                        f"{self.bucket_path}/raw_data_generated/{enc_doc['file'].replace('.txt','.png')}", 
                        content_type="image/png"
                    )
        print("Generating Lab Images...")
        for lab_doc in lab_docs:
            img_file = await self.generate_lab_img(
                lab_entry, 
                encounters[0].get("patient",{}).get("name"), 
                output_filename=f"{self.output_dir}/{lab_doc['file'].replace('.txt','.png')}"
            )
            if img_file:
                # Upload to GCS
                with open(img_file, "rb") as f:
                    self.gcs.create_file_from_string(
                        f.read(), 
                        f"{self.bucket_path}/raw_data_generated/{lab_doc['file'].replace('.txt','.png')}", 
                        content_type="image/png"
                    )
        print("Generating Imaging Report Images...")
        for img_doc in imaging_docs:
            img_file = await self.generate_imaging_report_img(
                img_doc["imaging_report_text"], 
                output_filename=f"{self.output_dir}/{img_doc['file'].replace('.txt','.png')}"
            )
            if img_file:
                # Upload to GCS
                with open(img_file, "rb") as f:
                    self.gcs.create_file_from_string(
                        f.read(), 
                        f"{self.bucket_path}/raw_data_generated/{img_doc['file'].replace('.txt','.png')}", 
                        content_type="image/png"
                    )

        print()
        res = {
            "conversation" : [
                    {
                    'sender': 'admin',
                    'message': 'Hello, this is Linda the Hepatology Clinic admin desk. How can I help you today?'
                    }
            ]
        }
        self.gcs.create_file_from_string(json.dumps(res, indent=4), f"patient_data/{self.args.get('patient_id')}/pre_consultation_chat.json", content_type="application/json")


class PreConsulteAgent(BaseLogicAgent):
    def __init__(self):
        super().__init__()  
        self.gcs = bucket_ops.GCSBucketManager(bucket_name="clinic_sim")

    def _get_available_slots(self):
        """
        Mock API call to get real-time availability.
        In a real app, this would query your calendar DB.
        """
        return {
            "doctorName": "Dr. A. Gupta",
            "specialty": "Hepatology",
            "slots": [
                {"slotId": "SLOT_10_AM", "date": "2025-12-10", "time": "09:30 AM", "type": "In-Person"},
                {"slotId": "SLOT_11_PM", "date": "2025-12-11", "time": "02:00 PM", "type": "In-Person"},
                {"slotId": "SLOT_12_AM", "date": "2025-12-12", "time": "10:00 AM", "type": "In-Person"}
            ]
        }

    async def pre_consulte_agent(self, user_request:dict, patient_id: str):
        # 1. Load Resources
        current_user_message = user_request.get("patient_message", "")
        user_attachments = user_request.get("patient_attachment", [])
        user_form_data = user_request.get("patient_form", {})

        with open("system_prompts/live_admin_agent.md", "r", encoding="utf-8") as f: 
            system_instruction = f.read()
        
        # Load the Strict JSON Schema
        with open("response_schema/pre_consult_admin.json", "r", encoding="utf-8") as f:
            response_schema = json.load(f)

        # Blank form template for SEND_FORM action
        with open("data/blank_pre_consult_form.json", "r", encoding="utf-8") as f:
            blank_form = json.load(f)

        # 2. Load History
        history_path = f"patient_data/{patient_id}/pre_consultation_chat.json"
        
        # Handle case where file doesn't exist (First run)
        try:
            chat_data = json.loads(self.gcs.read_file_as_string(history_path))
        except:
            # Initialize if missing
            chat_data = {"conversation": []}
            
        history = chat_data.get("conversation", [])

        # 3. Get External Data (Slots) to inject into context
        available_slots = self._get_available_slots()

        # 4. Construct Prompt Context
        # We inject the slots into the prompt so the LLM knows what to offer when the time comes
        # Build the latest input section including attachments
        latest_input = current_user_message
        if user_attachments:
            attachment_names = ", ".join(user_attachments) if isinstance(user_attachments, list) else str(user_attachments)
            latest_input += f"\n[Patient uploaded file(s): {attachment_names}]"
        if user_form_data:
            latest_input += f"\n[Patient submitted form data: {json.dumps(user_form_data)}]"

        prompt_content = (
            f"### CONVERSATION HISTORY (JSON) ###\n"
            f"{json.dumps(history, indent=2)}\n\n"
            f"### LATEST USER INPUT ###\n"
            f"{latest_input}\n\n"
            f"### AVAILABLE SLOTS (Use this data if Action is OFFER_SLOTS) ###\n"
            f"{json.dumps(available_slots, indent=2)}\n\n"
            f"### TASK ###\n"
            f"Determine the next state based on the history. Return the JSON response."
        )

        try:
            # 5. Call LLM with JSON Schema
            response = await self.client.aio.models.generate_content(
                model=MODEL_PRE_CONSULT, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema,
                    system_instruction=system_instruction, 
                    temperature=0.3
                )
            )
            
            # 6. Parse the LLM Response
            agent_response_obj = json.loads(response.text)

            # 7. Update History
            # Append User Message
            history.append({
                'sender': 'patient',
                'message': current_user_message,
                "attachments": user_attachments,
                "form_data": user_form_data
            })

            # Append Admin Response (The Full Object)
            # We strip null fields to keep the JSON clean
            if agent_response_obj.get("action_type") == "SEND_FORM":
                agent_response_obj['form_request'] = blank_form
                
            clean_response = {k: v for k, v in agent_response_obj.items() if v is not None}
            clean_response['sender'] = 'admin'
            
            history.append(clean_response)

            # 8. Save back to GCS
            chat_data["conversation"] = history
            self.gcs.create_file_from_string(
                json.dumps(chat_data, indent=4), 
                history_path, 
                content_type="application/json"
            )

            # Return the full object so the server/frontend can render forms/slots
            return agent_response_obj
            
        except Exception as e:
            print(f"Error in pre_consulte_agent: {e}") 
            # Fallback text error
            return {
                "message": "I apologize, the system is currently syncing. Please try again.",
                "action_type": "TEXT_ONLY"
            }



class RawDataProcessing(BaseLogicAgent):
    def __init__(self):
        super().__init__()  
        self.gcs = bucket_ops.GCSBucketManager(bucket_name="clinic_sim")

    
    async def get_text_doc(self, image_path: str):
        with open("system_prompts/image_parser.md", "r", encoding="utf-8") as f: 
            system_instruction = f.read()

        with open("response_schema/image_parser.json", "r", encoding="utf-8") as f:
            response_schema = json.load(f)

        prompt_text = "Analyze this image. 1. Classify the document type based on headers and content. 2. Extract all visible text verbatim."
            
        image_bytes = self.gcs.read_file_as_bytes(image_path)
        mime_type = "image/png"

        # Prepare content parts (Text + Image)
        contents = [
            prompt_text,
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        ]

        response = await self.client.aio.models.generate_content(
            model=MODEL, # Ensure this model supports Vision (e.g. gemini-1.5-flash)
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", 
                response_schema=response_schema, 
                system_instruction=system_instruction, 
                temperature=0.1 # Low temp for accurate OCR
            )
        )
        
        return json.loads(response.text)
    
    async def process_raw_data(self, patient_id: str):
        # pre_consult_chat_path = f"patient_data/{patient_id}/pre_consultation_chat.json"
        # content_str = self.gcs.read_file_as_string(pre_consult_chat_path)
        # history_data = json.loads(content_str)

        file_list = self.gcs.list_files("patient_data/P0001/raw_data/")

        results = []
        
        for att in file_list:
            file_path = f"patient_data/{patient_id}/raw_data/{att}"
            result = await self.get_text_doc(file_path)
            result.update({"source_file": att})
            results.append(result)
            print(f"Processed {att}: {result}")

        self.gcs.create_file_from_string(
            json.dumps(results, indent=4),
            f"patient_data/{patient_id}/parsed_raw_data.json",
            content_type="application/json"
        )

    async def get_raw_context(self, patient_id: str):
        raw_data = self.gcs.read_file_as_string(f"patient_data/{patient_id}/parsed_raw_data.json")
        raw_objects = json.loads(raw_data)

        pre_consultation_chat_path = self.gcs.read_file_as_string(f"patient_data/{patient_id}/pre_consultation_chat.json")
        pre_consultation_chat = json.loads(pre_consultation_chat_path)

        return {
            "raw_objects": raw_objects,
            "pre_consultation_chat": pre_consultation_chat
        }

    async def process_dashboard_content(self, patient_id):
        # 1. Define tasks
        tasks = [
            self.process_referral_board(patient_id),
            self.process_image_board(patient_id),
            self.process_encounter_board(patient_id),
            self.process_dashboard_patient_context(patient_id),
            self.process_dashboard_analysis_object(patient_id)
        ]

        # 2. Run in parallel
        results = await asyncio.gather(*tasks)

        # 3. Loop and print
        print("First batch of dashboard processing results:")
        for result in results:
            print(result)


        tasks = [
            self.dashboard_latest_labs(patient_id),
            self.dashboard_lab_chart_data(patient_id),
            self.dashboard_pre_diagnosis(patient_id),
            self.get_encounters_track(patient_id),
            self.get_medication_track(patient_id),
            self.get_lab_track(patient_id),
            self.get_risk_event_track(patient_id),
        ]

        # 2. Run in parallel
        results = await asyncio.gather(*tasks)

        # 3. Loop and print
        print("First batch of dashboard processing results:")
        for result in results:
            print(result)
            
        return results

    async def process_board_object(self, patient_id):
        file_list = self.gcs.list_files(f"patient_data/{patient_id}/board_items/")

        board_objects = []

        for file in file_list:
            file_path = f"patient_data/{patient_id}/board_items/{file}"
            raw_data = self.gcs.read_file_as_string(file_path)
            raw_objects = json.loads(raw_data)


            if "referral.json" in file:
                rec = {
                    "id": "referral-doctor-info",
                    "date" : raw_objects.get("date",""),
                    "visitType" : raw_objects.get("visitType",""),
                    "provider" : raw_objects.get("provider",""),
                    "specialty" : raw_objects.get("specialty",""),
                    "rawText" : raw_objects.get("rawText",""),
                    "dataSource" : raw_objects.get("dataSource",""),
                    "highlights" : raw_objects.get("highlights",[]),
                    "componentType"  : "RawClinicalNote",
                    "zone" : "referral-zone"
                }
                board_objects.append(rec)
                rec2 = {
                    "id": "referral-letter-image",
                    "date" : raw_objects.get("date",""),
                    "studyType" : raw_objects.get("studyType",""),
                    "provider" : raw_objects.get("provider",""),
                    "specialty" : raw_objects.get("specialty",""),
                    "imageUrl" : raw_objects.get("imageUrl",""),
                    "dataSource" : raw_objects.get("dataSource",""),
                    "componentType"  : "RadiologyImage",
                    "zone" : "referral-zone"
                }
                board_objects.append(rec2)
            elif "raw_images.json" in file:
                for r in raw_objects:
                    r['componentType'] = "RadiologyImage"
                    r['zone'] = "raw-ehr-data-zone"
                board_objects += raw_objects
            elif "encounters.json" in file:
                for i, e in enumerate(raw_objects):
                    e['id'] = f"single-encounter-{i+1}"
                    e['componentType'] = "SingleEncounterDocument"
                    e['zone'] = "data-zone"
                    board_objects.append(e)
            elif "patient_context.json" in file:
                raw_objects['id'] = "dashboard-item-patient-context"
                raw_objects['componentType'] = "PatientContext"
                raw_objects['zone'] = "adv-event-zone"
                board_objects.append(raw_objects)

                raw_objects['id'] = "sidebar-1"     
                raw_objects['componentType'] = "Sidebar"

                board_objects.append(raw_objects)
            elif "dashboard_analysis.json" in file:
                raw_objects['id'] = "adverse-event-analytics"
                raw_objects['componentType'] = "AdverseEventAnalytics"
                raw_objects['zone'] = "adv-event-zone"
                
                board_objects.append(raw_objects)
            elif "dashboard_lab_latest.json" in file:
                board_objects.append({
                    "id": "dashboard-item-lab-table",
                    "componentType": "LabTable",
                    "zone" : "adv-event-zone",
                    "labResults" : raw_objects
                })
            elif "dashboard_lab_chart.json" in file:
                board_objects.append({
                    "id": "dashboard-item-lab-chart",
                    "componentType": "LabChart",
                    "zone" : "adv-event-zone",
                    "chartData" : raw_objects
                })
            elif "dashboard_pre_diagnosis.json" in file:
                board_objects.append({
                    "id": "differential-diagnosis",
                    "componentType": "DifferentialDiagnosis",
                    "zone" : "adv-event-zone",
                    "differential" : raw_objects
                })
            elif "dashboard_encounters_track.json" in file:
                board_objects.append({
                    "id": "encounter-track-1",
                    "componentType": "EncounterTrack",
                    "zone" : "adv-event-zone",
                    "encounters" : raw_objects
                })
            elif "dashboard_medication_track.json" in file:
                board_objects.append({
                    "id": "medication-track-1",
                    "componentType": "MedicationTrack",
                    "zone" : "adv-event-zone",
                    "data" : raw_objects
                })
            elif "dashboard_lab_track.json" in file:
                board_objects.append({
                    "id": "lab-track-1",
                    "componentType": "LabTrack",
                    "zone" : "adv-event-zone",
                    "data" : raw_objects
                })
            elif "dashboard_risk_event_track.json" in file:
                board_objects.append({
                    "id": "risk-track-1",
                    "componentType": "RiskTrack",
                    "zone" : "adv-event-zone",
                    "risks" : raw_objects.get("risks")
                })
                board_objects.append({
                    "id": "key-events-track-1",
                    "componentType": "KeyEventsTrack",
                    "zone" : "adv-event-zone",
                    "events" : raw_objects.get("events")
                })
            
        self.gcs.create_file_from_string(
            json.dumps(board_objects, indent=4),
            f"patient_data/{patient_id}/board_objects.json",
            content_type="application/json"
        )

            


    async def process_referral_board(self, patient_id):

        try:
            raw_data = self.gcs.read_file_as_string(f"patient_data/{patient_id}/parsed_raw_data.json")
            raw_objects = json.loads(raw_data)

            referal_raw_object = None
            for raw in raw_objects:
                if raw.get('type') == "referral":
                    referal_raw_object = raw
                    break

            if not referal_raw_object:
                return {
                    "status": "no referral found"
                }
            
            referral_text = referal_raw_object.get("content","")

            with open("system_prompts/board_referral_parser.md", "r", encoding="utf-8") as f:
                system_instruction = f.read()

            # 2. Load Response Schema
            with open("response_schema/board_referral_parser.json", "r", encoding="utf-8") as f:
                response_schema = json.load(f)

            # 3. Prepare Prompt
            prompt_text = (
                "Analyze the following referral letter text. "
                "Extract the date, visit type, provider, study type, specialty, and data source. "
                "Populate the 'highlights' array with exact text snippets used to derive these values."
            )

            # 4. Prepare content parts (Instructions + The Raw Text)
            contents = [
                prompt_text,
                referral_text
            ]

            # 5. Call Model
            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.1 # Low temp for factual extraction
                )
            )

            

            result_obj = json.loads(response.text)
            result_obj['rawText'] = referral_text


            self.gcs.create_file_from_string(
                json.dumps(result_obj, indent=4),
                f"patient_data/{patient_id}/board_items/referral.json",
                content_type="application/json"
            )



            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in process_referral_board: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def process_image_board(self, patient_id):
        try:
            raw_data = self.gcs.read_file_as_string(f"patient_data/{patient_id}/parsed_raw_data.json")
            raw_objects = json.loads(raw_data)

            image_enc_id_count = 1
            image_lab_id_count = 1
            image_imaging_id_count = 1
            results = []
            for raw in raw_objects:
                if raw.get('type') == "encounter":
                    rec_id = f"raw-encounter-image-{image_enc_id_count}"
                    data_ = {
                        "id" : rec_id,
                        "imageUrl" : raw.get("source_file")
                    }
                    results.append(data_)
                    image_enc_id_count += 1

                elif raw.get('type') == "lab":
                    rec_id = f"raw-lab-image-{image_lab_id_count}"
                    data_ = {
                        "id" : rec_id,
                        "imageUrl" : raw.get("source_file")
                    }
                    results.append(data_)
                    image_lab_id_count += 1
                elif raw.get('type') == "imaging":
                    rec_id = f"raw-lab-image-radiology-{image_imaging_id_count}"
                    data_ = {
                        "id" : rec_id,
                        "imageUrl" : raw.get("source_file")
                    }
                    results.append(data_)
                    image_imaging_id_count += 1

            self.gcs.create_file_from_string(
                json.dumps(results, indent=4),
                f"patient_data/{patient_id}/board_items/raw_images.json",
                content_type="application/json"
            )

            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in process_image_board: {e}")
            return {
                "status": "error",
                "message": str(e)
            }
    
    async def process_encounter_board(self, patient_id):
        try:
            raw_data = self.gcs.read_file_as_string(f"patient_data/{patient_id}/parsed_raw_data.json")
            raw_objects = json.loads(raw_data)


            encounter_text = ""
            for raw in raw_objects:
                if raw.get('type') == "encounter":
                    encounter_text += "ENCOUNTER\n" +  raw.get("content", "") + "\n\n"

                

            with open("system_prompts/encounter_generator.md", "r", encoding="utf-8") as f: 
                system_instruction = f.read()
            with open("response_schema/encounter.json", "r", encoding="utf-8") as f:
                response_schema = json.load(f)

            # 3. Prepare Prompt
            prompt_text = (
                "Analyze the following encounter note text. "
                "Extract the date, visit type, provider, study type, specialty, and etc. "
            )

            # 4. Prepare content parts (Instructions + The Raw Text)
            contents = [
                prompt_text,
                encounter_text
            ]

            # 5. Call Model
            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.1 # Low temp for factual extraction
                )
            )

            

            result_obj = json.loads(response.text)

            

            self.gcs.create_file_from_string(
                json.dumps(result_obj, indent=4),
                f"patient_data/{patient_id}/board_items/encounters.json",
                content_type="application/json"
            )

            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in process_encounter_board: {e}")
            return {
                "status": "error",
                "message": str(e)
            }
    
    async def process_dashboard_patient_context(self, patient_id: str):
        try:
            with open("system_prompts/dashboard_patient_context.md", "r", encoding="utf-8") as f:
                system_instruction = f.read()

            # 2. Load Response Schema
            with open("response_schema/dashboard_patient_context.json", "r", encoding="utf-8") as f:
                response_schema = json.load(f)


            context_payload = await self.get_raw_context(patient_id)


            prompt_content = (
                f"### DATA SOURCES ###\n"
                f"{json.dumps(context_payload, indent=2)}\n\n"
                f"### INSTRUCTION ###\n"
                f"Analyze the data above to populate the Clinical Dashboard.\n"
                f"1. **Synthesize:** Combine the official history (Encounters) with new self-reported info (Chat).\n"
                f"2. **Medication Logic:** If the patient mentions taking OTC meds (like Tylenol) in the chat, ADD them to the medication timeline, estimating dates based on the conversation.\n"
                f"3. **Risk Assessment:** Determine risk based on the severity of symptoms described in the Chat (e.g., 'Yellow eyes' = High).\n"
                f"4. **Problem List:** Include both chronic conditions and the acute symptoms they are complaining about now."
            )

            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.2 # Low temperature for factual extraction
                )
            )
            result_obj = json.loads(response.text)
            self.gcs.create_file_from_string(
                json.dumps(result_obj, indent=4),
                f"patient_data/{patient_id}/board_items/patient_context.json",
                content_type="application/json"
            )


            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in proocess_dashboard_patient_context: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def process_dashboard_analysis_object(self, patient_id: str):
        try:
            # 1. Load System Instruction
            with open("system_prompts/dashboard_analysis.md", "r", encoding="utf-8") as f:
                system_instruction = f.read()

            # 2. Load Response Schema
            with open("response_schema/dashboard_analysis.json", "r", encoding="utf-8") as f:
                response_schema = json.load(f)

            # 3. Retrieve Context (Raw Data + Pre-Consult Data)
            # Assuming this method returns a dict with keys like 'labs', 'medications', 'chat_transcript', etc.
            context_payload = await self.get_raw_context(patient_id) 

            # 4. Construct Prompt
            prompt_content = (
                f"### PATIENT DATA CONTEXT ###\n"
                f"{json.dumps(context_payload, indent=2)}\n\n"
                f"### INSTRUCTION ###\n"
                f"Act as an expert Clinical Toxicologist and Hepatologist.\n"
                f"Analyze the provided patient data (History, Labs, Symptoms, Medications) to generate a safety analysis.\n\n"
                f"1. **Adverse Events:** Identify specific clinical events (e.g., Hepatitis, Rash) based on abnormal labs and symptoms.\n"
                f"2. **RUCAM Assessment:** Perform a causality assessment for the suspected drug-induced liver injury. Fill the table row-by-row based on standard RUCAM criteria (Time to onset, Risk factors, Exclusion of other causes).\n"
                f"3. **CTCAE Grading:** Grade the severity of lab abnormalities (ALT, AST, Bilirubin, INR) according to CTCAE v5.0 criteria.\n"
                f"4. **Reasoning:** Synthesize the findings into a cohesive clinical narrative justifying your scoring."
            )

            # 5. Call Model
            response = await self.client.aio.models.generate_content(
                model=MODEL,
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.1 # Very low temperature for precise scoring and grading
                )
            )
            result_obj = json.loads(response.text)
            self.gcs.create_file_from_string(
                json.dumps(result_obj, indent=4),
                f"patient_data/{patient_id}/board_items/dashboard_analysis.json",
                content_type="application/json"
            )

            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in process_dashboard_analysis_object: {e}")
            return {
                "status": "error",
                "message": str(e)
            }


    async def dashboard_latest_labs(self, patient_id: str):
        try:
            # 1. Load System Instruction
            with open("system_prompts/dashboard_lab_latest.md", "r", encoding="utf-8") as f:
                system_instruction = f.read()

            # 2. Load Response Schema
            with open("response_schema/dashboard_lab_latest.json", "r", encoding="utf-8") as f:
                response_schema = json.load(f)

            # 3. Retrieve Raw Patient Context
            context_payload = await self.get_raw_context(patient_id)

            # 4. Construct Prompt
            # We pass the raw data and ask it to filter for the most recent values only.
            prompt_content = (
                f"### PATIENT RECORDS ###\n"
                f"{json.dumps(context_payload, indent=2)}\n\n"
                f"### INSTRUCTION ###\n"
                f"Review the patient records above. Extract the LATEST available result for every distinct lab test found.\n"
                f"1. **Filter:** Ignore older entries if a newer one exists for the same test.\n"
                f"2. **Standardize:** Normalize test names (e.g., 'SGPT' -> 'ALT').\n"
                f"3. **Analyze:** Compare the value against the normal range to determine the 'status' (normal, high, low, critical).\n"
                f"4. **History:** If a previous value exists in the history for the same test, populate 'previousValue'."
            )

            # 5. Call Model
            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.0 # Zero temperature for strict factual extraction
                )
            )
            result_obj = json.loads(response.text)
            self.gcs.create_file_from_string(
                json.dumps(result_obj, indent=4),
                f"patient_data/{patient_id}/board_items/dashboard_lab_latest.json",
                content_type="application/json"
            )

            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in dashboard_latest_labs: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def dashboard_lab_chart_data(self, patient_id: str):
        try:
            # 1. Load System Instruction
            with open("system_prompts/dashboard_lab_chart.md", "r", encoding="utf-8") as f:
                system_instruction = f.read()

            # 2. Load Response Schema
            with open("response_schema/dashboard_lab_chart.json", "r", encoding="utf-8") as f:
                response_schema = json.load(f)

            # 3. Retrieve Raw Patient Context
            context_payload = await self.get_raw_context(patient_id)

            # 4. Construct Prompt
            prompt_content = (
                f"### PATIENT DATA CONTEXT ###\n"
                f"{json.dumps(context_payload, indent=2)}\n\n"
                f"### INSTRUCTION ###\n"
                f"Analyze the patient data to build historical trend lines for laboratory values.\n"
                f"1. **Extraction:** Identify every lab test that has at least one result.\n"
                f"2. **Aggregation:** Group results by biomarker name (e.g., collect all 'ALT' values together).\n"
                f"3. **Chronology:** Sort the data points inside each biomarker group by date (Oldest -> Newest).\n"
                f"4. **Details:** For every data point, extract the Date, Value, and the specific Unit of measurement (e.g., 'U/L', 'mg/dL').\n"
                f"5. **Standardization:** Convert all dates to 'YYYY-MM-DD'. Ensure values are numbers.\n"
                f"6. **Key Focus:** Prioritize Liver Function Tests (ALT, AST, Bilirubin, INR) but include others if found."
            )

            # 5. Call Model
            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.0 # Zero temp for strict extraction
                )
            )
            result_obj = json.loads(response.text)
            self.gcs.create_file_from_string(
                json.dumps(result_obj, indent=4),
                f"patient_data/{patient_id}/board_items/dashboard_lab_chart.json",
                content_type="application/json"
            )

            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in dashboard_lab_chart_data: {e}")
            return {
                "status": "error",
                "message": str(e)
            }
    
    async def dashboard_pre_diagnosis(self, patient_id: str):
        try:
            # 1. Load System Instruction
            with open("system_prompts/dashboard_pre_diagnosis.md", "r", encoding="utf-8") as f:
                system_instruction = f.read()

            # 2. Load Response Schema
            with open("response_schema/dashboard_pre_diagnosis.json", "r", encoding="utf-8") as f:
                response_schema = json.load(f)

            # 3. Retrieve Raw Patient Context
            context_payload = await self.get_raw_context(patient_id)

            # 4. Construct Prompt
            prompt_content = (
                f"### PATIENT DATA CONTEXT ###\n"
                f"{json.dumps(context_payload, indent=2)}\n\n"
                f"### INSTRUCTION ###\n"
                f"Act as an expert Diagnostician. Analyze the patient's symptoms, history, and laboratory results.\n"
                f"1. **Differential Diagnosis:** Generate a list of potential diagnoses that explain the clinical presentation.\n"
                f"2. **Probability Assessment:** Assign a probability status (low, medium, high) to each based on the strength of the evidence.\n"
                f"   - **High:** Supported by specific lab values or pathognomonic symptoms.\n"
                f"   - **Medium:** Plausible but lacks definitive confirmation or shares symptoms with other conditions.\n"
                f"   - **Low:** Unlikely but cannot be fully ruled out without further testing.\n"
                f"3. **Reasoning:** In the 'note', briefly explain *why* you chose that diagnosis and status, citing specific data points (e.g., 'ALT > 1000', 'History of alcohol use')."
            )

            # 5. Call Model
            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.1 # Low temp for evidence-based reasoning
                )
            )
            result_obj = json.loads(response.text)
            self.gcs.create_file_from_string(
                json.dumps(result_obj, indent=4),
                f"patient_data/{patient_id}/board_items/dashboard_pre_diagnosis.json",
                content_type="application/json"
            )
            
            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in dashboard_pre_diagnosis: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def get_encounters_track(self, patient_id: str):
        try:
            # 1. Load System Instruction
            with open("system_prompts/dashboard_encounters_track.md", "r", encoding="utf-8") as f:
                system_instruction = f.read()

            # 2. Load Response Schema
            with open("response_schema/dashboard_encounters_track.json", "r", encoding="utf-8") as f:
                response_schema = json.load(f)

            # 3. Retrieve Raw Patient Context
            # This payload contains the raw notes, previous encounters, and history
            context_payload = await self.get_raw_context(patient_id)

            encounters_parsed_text = self.gcs.read_file_as_string(f"patient_data/{patient_id}/board_items/encounters.json")
            encounters_object = json.loads(encounters_parsed_text)

            # 4. Construct Prompt
            prompt_content = (
                f"### PATIENT RECORDS ###\n"
                f"{json.dumps(context_payload, indent=2)}\n\n"
                f"### Encounters parsed ###\n"
                f"{json.dumps(encounters_object, indent=2)}\n\n"
                f"### INSTRUCTION ###\n"
                f"Act as a Medical Historian. Construct a chronological timeline of patient encounters based on the records provided.\n"
                f"1. **Extraction:** identify all distinct interactions (Clinic Visits, Telehealth, ER Admissions, Discharge).\n"
                f"2. **Ordering:** Sort them strictly by date (Oldest -> Newest) and assign a sequential `encounter_no`.\n"
                f"3. **Details:** Extract the Provider, Chief Complaint, Impression/Diagnosis, and Differential Diagnosis for each visit.\n"
                f"4. **Medications:** List medications specifically *started*, *stopped*, or *modified* during that encounter.\n"
                f"5. **Narrative Link (Casual Reason):** For the `casual_reason` field, explain the clinical significance of this encounter in the context of the patient's *current* major issue (e.g., how did this visit contribute to the progression of the disease, missed diagnosis, or treatment timeline?)."
            )

            # 5. Call Model
            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.1 # Low temp for factual accuracy
                )
            )
            result_obj = json.loads(response.text)
            self.gcs.create_file_from_string(
                json.dumps(result_obj, indent=4),
                f"patient_data/{patient_id}/board_items/dashboard_encounters_track.json",
                content_type="application/json"
            )

            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in get_encounters_track: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def get_medication_track(self, patient_id: str):
        try:
            # 1. Load System Instruction
            with open("system_prompts/dashboard_medication_track.md", "r", encoding="utf-8") as f:
                system_instruction = f.read()

            # 2. Load Response Schema
            with open("response_schema/dashboard_medication_track.json", "r", encoding="utf-8") as f:
                response_schema = json.load(f)

            # 3. Retrieve Contexts
            # Get raw data (notes, labs, etc.)
            context_payload = await self.get_raw_context(patient_id)
            
            # Get the structured encounters list created by the previous agent
            encounters_parsed_text = self.gcs.read_file_as_string(f"patient_data/{patient_id}/board_items/encounters.json")
            encounters_object = json.loads(encounters_parsed_text)

            # 4. Construct Prompt
            # We provide the structured encounters to help the AI map medications to specific dates/visits
            prompt_content = (
                f"### RAW PATIENT RECORDS ###\n"
                f"{json.dumps(context_payload, indent=2)}\n\n"
                f"### STRUCTURED ENCOUNTERS TIMELINE ###\n"
                f"{json.dumps(encounters_object, indent=2)}\n\n"
                f"### INSTRUCTION ###\n"
                f"Act as an expert Clinical Pharmacist. Construct a medication timeline based on the records provided.\n"
                f"1. **Encounters Reference:** First, extract the simplified list of encounters (Number and Date) to serve as the timeline X-axis.\n"
                f"2. **Medication Extraction:** Identify all medications mentioned in the records.\n"
                f"3. **Timeline Logic (Start/End):**\n"
                f"   - **Start Date:** Use the date of the encounter where the drug was prescribed or first reported.\n"
                f"   - **End Date:** Calculate based on duration (e.g., '10-day course' started Oct 30 -> End Nov 09). If the med is ongoing or PRN, set end date to the date of the *latest* encounter or null if unknown.\n"
                f"4. **Details:** Extract Dose and Indication (reason for taking).\n"
                f"5. **Over the Counter:** Do not ignore OTC drugs (like Acetaminophen/Tylenol) if the patient mentions taking them."
            )

            # 5. Call Model
            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.0 # Zero temp for precise date calculation
                )
            )
            result_obj = json.loads(response.text)
            self.gcs.create_file_from_string(
                json.dumps(result_obj, indent=4),
                f"patient_data/{patient_id}/board_items/dashboard_medication_track.json",
                content_type="application/json"
            )
            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in get_medication_track: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def get_lab_track(self, patient_id: str):
        try:
            # 1. Load System Instruction
            with open("system_prompts/dashboard_lab_track.md", "r", encoding="utf-8") as f:
                system_instruction = f.read()

            # 2. Load Response Schema
            with open("response_schema/dashboard_lab_track.json", "r", encoding="utf-8") as f:
                response_schema = json.load(f)

            # 3. Retrieve Raw Patient Context
            context_payload = await self.get_raw_context(patient_id)

            # 4. Construct Prompt
            prompt_content = (
                f"### PATIENT RECORDS ###\n"
                f"{json.dumps(context_payload, indent=2)}\n\n"
                f"### INSTRUCTION ###\n"
                f"Act as a Clinical Data Specialist. Extract a longitudinal track of laboratory values.\n"
                f"1. **Grouping:** Group all results by the specific biomarker name (e.g., combine 'SGPT' and 'ALT').\n"
                f"2. **Metadata:** For each biomarker, identify the measurement `unit` and the `referenceRange` (min/max) used in the reports.\n"
                f"3. **Data Points:** For every test result found, extract:\n"
                f"   - The value (as a number).\n"
                f"   - The precise timestamp `t` in ISO 8601 format (YYYY-MM-DDTHH:MM:SS).\n"
                f"4. **Time Handling:** If the specific time (HH:MM:SS) is not mentioned in the report, assume '00:00:00' or the general time of the visit.\n"
                f"5. **Sorting:** Ensure the `values` array is sorted chronologically (Oldest -> Newest)."
            )

            # 5. Call Model
            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.0 # Zero temp for precise number/date extraction
                )
            )
            result_obj = json.loads(response.text)
            self.gcs.create_file_from_string(
                json.dumps(result_obj, indent=4),
                f"patient_data/{patient_id}/board_items/dashboard_lab_track.json",
                content_type="application/json"
            )
            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in get_lab_track: {e}")
            return {
                "status": "error",
                "message": str(e)
            }

    async def get_risk_event_track(self, patient_id: str):
        try:
            # 1. Load System Instruction
            with open("system_prompts/dashboard_risk_event_track.md", "r", encoding="utf-8") as f:
                system_instruction = f.read()

            # 2. Load Response Schema
            with open("response_schema/dashboard_risk_event_track.json", "r", encoding="utf-8") as f:
                response_schema = json.load(f)

            # 3. Retrieve Raw Patient Context
            context_payload = await self.get_raw_context(patient_id)
            encounters_parsed_text = self.gcs.read_file_as_string(f"patient_data/{patient_id}/board_items/encounters.json")
            encounters_object = json.loads(encounters_parsed_text)
            # 4. Construct Prompt
            prompt_content = (
                f"### PATIENT RECORDS ###\n"
                f"{json.dumps(context_payload, indent=2)}\n\n"
                f"### STRUCTURED ENCOUNTERS TIMELINE ###\n"
                f"{json.dumps(encounters_object, indent=2)}\n\n"
                f"### INSTRUCTION ###\n"
                f"Act as a Clinical Risk Manager. Analyze the patient records to generate two aligned timelines: Risk Progression and Key Events.\n\n"
                f"1. **Risk Analysis:** For every significant encounter or date, calculate a `riskScore` (0-10 scale) based on clinical severity.\n"
                f"   - **1-3 (Low):** Stable, minor infection, baseline.\n"
                f"   - **4-6 (Medium):** New unexplained symptoms, polypharmacy, medication changes.\n"
                f"   - **7-10 (High):** Critical lab values, organ dysfunction signs (jaundice), emergency visits.\n\n"
                f"2. **Event Extraction:** Identify pivotal moments that drove the clinical narrative.\n"
                f"   - Include: Medication starts/stops, Symptom onset (subjective), Lab spikes (objective), Referrals.\n"
                f"   - Provide a concise `note` explaining the context.\n\n"
                f"3. **Timestamps:** Extract precise timestamps `t` (ISO 8601). If only a date is available, use T00:00:00 or an estimated time based on the visit type."
            )

            # 5. Call Model
            response = await self.client.aio.models.generate_content(
                model=MODEL, 
                contents=prompt_content,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    response_schema=response_schema, 
                    system_instruction=system_instruction, 
                    temperature=0.1 # Low temp for consistent scoring and date extraction
                )
            )
            result_obj = json.loads(response.text)
            self.gcs.create_file_from_string(
                json.dumps(result_obj, indent=4),
                f"patient_data/{patient_id}/board_items/dashboard_risk_event_track.json",
                content_type="application/json"
            )
            
            return {
                "status": "success"
            }
        except Exception as e:
            print(f"Error in get_risk_event_track: {e}")
            return {
                "status": "error",
                "message": str(e)
            }


