import requests as req
import threading
import unicodedata
import re
# Needed for branch reset regex
import regex
import json
from time import time
from threading import Lock
from io import StringIO
from bs4 import BeautifulSoup, SoupStrainer
from lxml import etree

catalog_id = 20 # 2019-2020 course catalog
chunk_size = 200 # max number of course ids per GET request
acalog_api_key = "3eef8a28f26fb2bcc514e6f1938929a1f9317628"
dev_output_files = True

ACALOG_COURSE_FIELDS = {
    "department": "acalog-field-486",
    "level": "acalog-field-488",
    "full_name": "acalog-field-490",
    "description": "acalog-field-471",
    "raw_precoreqs": "acalog-field-473",
    "offer_frequency": "acalog-field-475",
    "cross_listed": "acalog-field-478",
    "graded": "acalog-field-480",
    "credit_hours": "acalog-field-482",
    "contact_lecture_lab_hours": "acalog-field-484",
}
USED_FIELDS = {
    "id": True, # custom
    "department": True,
    "level": True,
    "full_name": True,
    "short_name": True, # custom, requires department and level to be true
    "description": True,
    "prerequisites": True, # custom
    "corequisites": False, # custom
    "raw_precoreqs": True, #If either prereq or coreq is true, then this must be true cause the client needs to look at this field to understand the other two
    "offer_frequency": True,
    "cross_listed": True,
    "graded": True,
    "credit_hours": True,
    "contact_lecture_lab_hours": True,
}
SETTINGS = {
    "show_raw_precoreqs": True
}
COURSE_DETAIL_TIMEOUT = 120.00 # seconds

allow_for_extension_regex = re.compile("(<catalog.*?>)|(<\/catalog>)|(<\?xml.*?\?>)")
prolog_and_root_ele_regex = re.compile("^(?P<prolog><\?xml.*?\?>)?\s*(?P<root><catalog.*?>)")

# group the most specific regex patterns first, then the more general ones for last
# goal is to capture classes that are loosely of the form "Prerequisites: [CAPTURE COURSE LISTINGS TEXT]",
# but does not capture classes explicitly stated to be corequisites. Tries to remove
# periods, trailing and leading space.
explicit_prereqs_include_syntax_regex = "(?:^\s*Prerequisites? include:?\s?(.*))"
explicit_prereqs_preference_syntax_regex = "(?:^\s*Prerequisites? preferences?:?\s*(.*))"
explicit_prereqs_explicit_or_coreqs_syntax_regex = "(?:^\s*Prerequisites? or Corequisites?:?\s?(.*))"
explicit_prereqs_implicit_or_coreqs_syntax_regex = "(?:^Prerequisites?\/Corequisites?:?\s*(.*))"
explicit_prereqs_before_coreqs_syntax_regex = "(?:^\s*Prerequisites?.*?:\s?(.*?(?=\W*Coreq)))"
explicit_prereqs_sequence_syntax_regex = "(?:\s*Prerequisites?:?\s*(.+(?=[\. ;,])*))"
# https://stackoverflow.com/questions/406230/regular-expression-to-match-a-line-that-doesnt-contain-a-word
# doesn't contain a "prerequisites:" sort of string
# if there is leading space then it will be captured. I'm not sure how to not capture it.
implicit_prereqs_syntax_regex = "(^((?!(Corequisite)).)*$)"
implicit_prereqs_before_coreqs_syntax_regex = "(?:^\s*(.+?(?=\W*Coreq)))"
full_prereqs_regex = "|".join([
    explicit_prereqs_include_syntax_regex,
    explicit_prereqs_preference_syntax_regex,
    explicit_prereqs_explicit_or_coreqs_syntax_regex,
    explicit_prereqs_implicit_or_coreqs_syntax_regex,
    explicit_prereqs_before_coreqs_syntax_regex,
    explicit_prereqs_sequence_syntax_regex,
    implicit_prereqs_syntax_regex,
    implicit_prereqs_before_coreqs_syntax_regex
])
# https://stackoverflow.com/a/44463324/8088388
branch_reset_prereqs_regex = regex.compile(f"(?|{full_prereqs_regex})", flags=regex.IGNORECASE|regex.DOTALL)
coreqs_regex = re.compile("Prerequisites?:?\s*(?P<prereqs>.*?(?=\W*Coreq))", flags=regex.IGNORECASE)

def dwrite_obj(obj, name):
    with open(name, "w+") as file:
        file.write(obj.__str__())

def dwrite_text(text, name):
    with open(name, "w+") as file:
        file.write(text)

def dwrite_utf8_file(text, name):
    with open(name, "w+", encoding='utf-8') as file:
        file.write(text)

# todo:
#       - [ ] need to add to requirements.txt or if virtualenv is ever set up, lxml, requests(? not sure if standard)
#       - [ ] need to dynamically fetch catalog id
#       - [ ] documentation??
#       - [ ] get rid of remaining part using beautifulsoup?
#       - [ ] refactor so each json retains the order of properties as specified in USED_FIELDS

class acalog_client():
    def __init__(self, api_key):
        self.search_endpoint = "http://rpi.apis.acalog.com/v2/search/courses"
        self.course_detail_endpoint = "http://rpi.apis.acalog.com/v2/content?options[full]=1&method=getItems&type=courses"
        self.api_key = api_key
        self.api_response_format = "xml"
        self._lock = Lock()
        self._course_details_xml_strs = []
        self._xml_prolog = ""
        self._catalog_root = ""

    # https://stackoverflow.com/a/34669482/8088388
    def _clean_utf(self, string):
        # unicodedata.normalize("NFKD", string)
        # Format the unicode string into its normalized combined version,
        # and get rid of unprintable characters
        return unicodedata.normalize("NFKC", string)
        # return unicodedata.normalize("NFKC", string).encode('cp1252','replace').decode('cp1252')

    def _all_threads_joined(self, threads):
        for thread in threads:
            thread.join(COURSE_DETAIL_TIMEOUT)
            if (thread.is_alive()):
                return False
        return True

    def get_course_ids_xml(self):
        return req.get(f"{self.search_endpoint}?key={self.api_key}&format={self.api_response_format}&method=listing&catalog={catalog_id}&options[limit]=0").content

    def _get_course_details(self, id_params):
        course_details_xml_str = req.get(f"{self.course_detail_endpoint}&key={self.api_key}&format={self.api_response_format}&catalog=20&{id_params}").content.decode("utf8")
        with self._lock:
            # https://stackoverflow.com/questions/39119165/xml-what-does-that-question-mark-mean
            # Can only have one prolog per XML document in order for it to be well-formed.
            # Can also only have one root.
            # Don't see a reason to keep it as a bytestring, beautifulsoup
            # is eventually just going to convert it to utf8 anyway
            match = prolog_and_root_ele_regex.match(course_details_xml_str)
            if (match is None):
                raise Error("XML document is missing prolog and root. Invalid.")
            self._xml_prolog = match.group("prolog") if match.group("prolog") is not None else '<?xml version="1.0"?>'
            if match.group("root") is None:
                raise Error("XML document is missing root element. Invalid.")
            self._catalog_root = match.group("root")
            self._course_details_xml_strs.append(allow_for_extension_regex.sub("", course_details_xml_str))


    def _course_xml_ids_to_url_params(self, all_ids_xml):
        only_ids = SoupStrainer("id")
        id_tags = BeautifulSoup(all_ids_xml, "xml", parse_only=only_ids).find_all("id")
        return [f"ids[]={id_xml.text}" for id_xml in id_tags]

    def get_all_courses_xml(self, course_ids_url_params):
        course_count = len(course_ids_url_params)
        id_chunks = [course_ids_url_params[i:i+chunk_size] for i in range(0, course_count, chunk_size)]
        chunk_count = len(id_chunks)
        courses_xml = ""
        thread_jobs = []
        for id_chunk in id_chunks:
            query_param_chunk = "&".join(id_chunk)
            fetch_course_details_job = threading.Thread(target=self._get_course_details, args=[query_param_chunk], daemon=False)
            thread_jobs.append(fetch_course_details_job)
            fetch_course_details_job.start()
        # Wait until all jobs are finished
        while True:
            if self._all_threads_joined(thread_jobs):
                break
        courses_xml = self._xml_prolog + self._catalog_root + "".join(self._course_details_xml_strs) + "</catalog>"
        return courses_xml

    def _extract_prereq_from_precoreq_str(self, precoreqs):
        match = regex.search(branch_reset_prereqs_regex, precoreqs)
        if (match is not None):
            if len(match.groups()) > 0:
                return match.groups()[0]
        return ""

    def _extract_prereqs_from_prereq_str(self, prereq_str):
        # All course department codes are of the form: 4 capital letters + "-" + 4 numbers
        # Will a department code ever not be exactly 4 capital letters? Possibly!
        course_short_names = []
        # https://stackoverflow.com/questions/4697882/how-can-i-find-all-matches-to-a-regular-expression-in-python
        for course_short_name in re.findall("([A-Z]{4} \d{4})", prereq_str):
            # Frontend uses the format AAAA-1111 so replace space with dash
            course_short_names.append(course_short_name.replace(" ", "-"))
        return course_short_names

    def _is_actual_course(self, course_xml_element):
        # A valid course should have a name and description (can be empty, just not missing).
        # For some reason, the <course> tag is used in some places where there isn't an actual course.
        # If you remove this check in _get_all_courses, the returned course count is 1939 when using
        # catalog id 20. But if you count the course ids from the API it's 1933.
        # This check correctly returns the 1933 courses.
        desc_xml = course_xml_element.xpath(f"*[local-name() = 'field'][@type='{ACALOG_COURSE_FIELDS['description']}']")
        name_xml = course_xml_element.xpath(f"*[local-name() = 'name']")
        return desc_xml and name_xml

    def _get_all_courses(self, courses_xml_str):
        parser = etree.XMLParser(encoding="utf-8")
        tree = etree.parse(StringIO(courses_xml_str), parser=parser)
        # https://stackoverflow.com/a/4256011/8088388
        course_content_xml = tree.getroot().xpath("//*[local-name() = 'course']/*[local-name() = 'content']")
        courses = []
        for raw_course in course_content_xml:
            if (self._is_actual_course(raw_course)):
                field_values = {}
                used_standard_fields = filter(lambda key: key in ACALOG_COURSE_FIELDS and USED_FIELDS[key], USED_FIELDS)
                used_custom_fields = filter(lambda key: key not in ACALOG_COURSE_FIELDS and USED_FIELDS[key], USED_FIELDS)
                for field_name in used_standard_fields:
                    # A <field>, like description, can have multiple children tags, so get all text nodes.
                    # One example of this is ARCH 4870 - Sonics Research Lab 1, catalog 20, courseid 38592
                    value = ("".join(raw_course.xpath(f"*[local-name() = 'field'][@type='{ACALOG_COURSE_FIELDS[field_name]}']//text()"))).replace("\n", "").replace("\r","").strip()
                    if field_name == 'description':
                        field_values['description'] = self._clean_utf(value).encode("utf8").decode("utf8")
                    else:
                        field_values[field_name] = self._clean_utf(value)
                for field_name in used_custom_fields:
                    if field_name == 'id':
                        course_id = re.search("(?P<id>\d+)$", raw_course.getparent().attrib["id"]).group("id")
                        field_values['id'] = course_id
                    elif field_name == 'prerequisites':
                        if 'raw_precoreqs' in field_values:
                            prereq_str = self._clean_utf(self._extract_prereq_from_precoreq_str(field_values['raw_precoreqs']))
                            prereqs = self._extract_prereqs_from_prereq_str(prereq_str)
                            field_values['prerequisites'] = prereqs
                    elif field_name == 'corequisites':
                        raise Error("Unimplemented")
                    elif field_name == 'short_name':
                        if 'department' in field_values and 'level' in field_values:
                            field_values['short_name'] = field_values['department'] + '-' + field_values['level']
                if (len(field_values) > 0):
                    courses.append(field_values)
        return courses

    def get_all_courses(self):
        ids_xml = self.get_course_ids_xml()
        ids = self._course_xml_ids_to_url_params(ids_xml)
        courses_xml_str = self.get_all_courses_xml(ids)
        if dev_output_files:
            parser = etree.XMLParser(remove_blank_text=True)
            tree = etree.parse(StringIO(courses_xml_str), parser)
            tree.write("courses20.xml", pretty_print=True)
        return self._get_all_courses(courses_xml_str)

def main():
    c = acalog_client(acalog_api_key)
    courses = c.get_all_courses()
    if dev_output_files:
        dwrite_utf8_file(json.dumps(courses, indent=4), "courses20.json")

if __name__ == "__main__":
    main()