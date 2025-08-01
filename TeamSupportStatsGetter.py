import argparse
import base64
import re
import time
import urllib
from copy import deepcopy
from dataclasses import dataclass, field
from functools import reduce
from pathlib import Path
import datetime
import json
import pytz

import dateutil.parser
import requests
import html2text

from cadis_bdk_contacts import CADIS_BDK_CONTACTS

LOAD_INCREMENT_IN_DAYS = 14

RETRY_INTERVAL_SECONDS = 60

STATUS_TO_EXCLUDE_FROM_RESOLUTION_TIME = ["Closed","Closed - NR","Spam/Junk Delete","Closed - Resolved","On Hold","Waiting on Customer"]

STATUS_CHANGE_RE = "Changed status from (?:&apos;|')(.*)(?:&apos;|') to (?:&apos;|')(.*)(?:&apos;|')"
STATUS_CHANGE_TO_RESOLVED_RE = "Changed status from (?:&apos;|')(?!Closed)(.*)(?:&apos;|') to (?:&apos;|')Closed(.*)(?:&apos;|')"
GROUP_ASSIGN_RE = "Reassigned.*from group (?:&apos;|')(.*)(?:&apos;|') to group (?:&apos;|')(.*)(?:&apos;|')"
USER_ASSIGN_RE = "Reassigned.*from user (?:&apos;|')(.*)(?:&apos;|') to user (?:&apos;|')(.*)(?:&apos;|')"
SEVERITY_CHANGE_RE = "Changed severity from (?:&apos;|')(.*)(?:&apos;|') to (?:&apos;|')(.*)(?:&apos;|')"
EMAIL_REPLY_RE = 'Changed status of ticket to (?:&quot;|")(.*)(?:&quot;|") since ticket was replied to via e-mail'
EXCLUDE_BY_NAME_RES = [r"\[RECOVERY\]"]

KEEP_RAW_ACTION_TEXT = False # if true the text is kept in its raw state (MAX_ACTION_TEXT_LEN is ignored)
GET_ATTACHEMNTS = False # download and save files
MAX_ACTION_TEXT_LEN = 1000

SEVERITY_POS_MAP = {
    "Normal": 0,
    "High": 1,
    "Critical": 2,
    "Medium": -1,
    "Low": -2
}



class DataStore(object):
    """
    DataStore is a very simple data storage provider that can hold data retrieved from cadis in a set of json files.
    For production, it is highly recommended to use a proper database to hold retrieved data.
    DataStore requires all records have a 'lastModificationTimestamp' and a 'uniqueIdentifier' field.
    When adding data it will always overwrite records with the same 'uniqueIdentifier' if the added record is newer
    according to 'lastModificationTimestamp'.
    """

    def __init__(self, directory: str, name: str) -> None:
        """
        Initialize a data store json file in the directory. If the json file already exists it is read.
        If not the file and file are created.
        @param directory: The directory where the json files reside
        @param name: The name of data source. The data will be stored in a file named <name>.json
        """
        self.directory = directory
        self.name = name
        self.filename = Path(directory, f'{name}.json').absolute()
        self.data = []
        self.newest_record_timestamp = None
        self.oldest_record_timestamp = None

        self.__create_or_load()

    def __create_or_load(self):
        if Path(self.filename).is_file():
            print(f'Opening existing file {self.filename}')
            try:
                f = open(self.filename, 'r')
                self.data = json.load(f)
                f.close()
                if len(self.data) > 0:
                    self.__deduplicate_data()
                    self.__build_metadata()

            except json.JSONDecodeError as e:
                print(f'{self.filename} is not a valid json file. {str(e)}')
                exit(2)
        else:  # create new filename
            print(f'Creating new data file at {self.filename}')
            try:
                Path(self.directory).mkdir(parents=True, exist_ok=True)
                self.data = []
                self.__store_data()
            except Exception as e:
                print(f'Error creating mew data file at {self.filename}, {str(e)}')
                exit(2)

    def __build_metadata(self):
        self.newest_record_timestamp = dateutil.parser.isoparse('2023-01-01T00:00:00.000000Z')
        self.oldest_record_timestamp = datetime.datetime.now(tz=pytz.utc)
        for i in range(0, len(self.data)):
            self.newest_record_timestamp = max(self.newest_record_timestamp,
                                               dateutil.parser.isoparse(self.data[i]["DateModified"]))
            self.oldest_record_timestamp = min(self.oldest_record_timestamp,
                                               dateutil.parser.isoparse(self.data[i]["DateModified"]))
        pass

    def __deduplicate_data(self):
        """
        Run through all data records and remove entries with the same uniqueIdentifier.
        The entry with the lager 'lastModificationTimestamp' will be retained.
        """
        uniq_recs = {}
        for e in self.data:
            pe = uniq_recs.get(e['ID'], None)
            if pe and pe['DateModified'] > e['DateModified']:
                continue
            uniq_recs[e['ID']] = e
        self.data = list(uniq_recs.values())

    def __store_data(self):
        print(f'Saving {len(self.data)} records to {self.filename}')

        f = open(self.filename, 'w')
        json.dump(self.data, f, indent=2)
        f.close()

    def add_data(self, new_data: list) -> None:
        """
        Add data records. Newer records will replace older ones if they have the same uniqueIdentifier
        @param new_data: List of new data records to add.
        """

        self.data += new_data
        self.__deduplicate_data()
        self.__build_metadata()
        self.__store_data()


@dataclass
class StatisticsDayRecord:
    date: str
    ticket_id: str
    ticket_nr: str
    actions: int = 0
    public_actions: int = 0
    start_assigned_group: str = "Unassigned"
    end_assigned_group: str = "Unassigned"
    start_assigned_user: str = "Unassigned"
    end_assigned_user: str = "Unassigned"
    did_escalate_severity: bool = False
    did_escalate_cs: bool = False
    did_escalate_management: bool = False
    start_severity_txt: str = "Normal"
    start_severity_pos: int = 0
    end_severity_txt: str = "Normal"
    end_severity_pos: int = 0
    time_spent_min: int = 0
    start_status: str = "New"
    end_status: str = "New"
    did_resolve: bool = False
    total_days_open: int = 0
    weekdays_days_since_last_contact: int = 0
    adjusted_resolution_days: int = 0
    adjusted_resolution_weekdays: int = 0

    cw: str = field(init=False)
    weekday: int = field(init=False)
    ID: str = field(init=False)
    DateModified: str = datetime.datetime.now(tz=pytz.utc).isoformat()

    def __post_init__(self):
        self.date = self.date[:10]
        self.did_resolve = self.end_status.startswith("Closed") and not self.start_status.startswith("Closed")
        self.ID = f'{self.ticket_id}_{self.date}'
        self.cw = cw_string_from_isodate(self.date)
        self.weekday = dateutil.parser.isoparse(self.date).isoweekday()

    def update_stats(self):
        self.did_resolve = self.end_status.startswith("Closed") and not self.start_status.startswith("Closed")
        self.did_escalate_cs = (self.did_escalate_cs or (('Customer Success' not in self.start_assigned_group)
                                                         and 'Customer Success' in self.end_assigned_group))
        self.did_escalate_severity = self.start_severity_pos < self.end_severity_pos

    def to_dict(self):
        r = {}
        for k, v in self.__dict__.items():
            if k.endswith("__"):
                continue
            r[k] = v

        return r

    def ingest_action(self, act: dict):
        self.actions += 1
        if act["IsVisibleOnPortal"] == "True":
            self.public_actions += 1
            self.weekdays_days_since_last_contact = 0
        self.time_spent_min += int(act["TimeSpent"] or 0)
        pass

    def ingest_history(self, hist: dict):
        if m := re.match(STATUS_CHANGE_RE, hist["Description"]):
            old_s = m.group(1)
            new_s = m.group(2)
            if not self.end_status == old_s:
                print(
                    f"WARNING: Ticket #{self.ticket_nr} inconsistent history status action. "
                    f"Ticket status should be '{self.end_status}' but"
                    f"history says change is from '{old_s}' to '{new_s}'")
            self.end_status = new_s
            self.update_stats()
        if m := re.match(EMAIL_REPLY_RE, hist["Description"]):
            new_s = m.group(1)
            self.end_status = new_s
            self.update_stats()
        elif m := re.match(GROUP_ASSIGN_RE, hist["Description"]):
            old_s = m.group(1)
            new_s = m.group(2)
            if (not self.end_assigned_group == old_s) and "1st" not in old_s:
                print(
                    f"WARNING: Ticket #{self.ticket_nr} inconsistent history group assign  action. Ticket group "
                    f"should be '{self.end_assigned_group}' but"
                    f"history says change is from '{old_s}' to '{new_s}'")
            self.end_assigned_group = new_s
            if "Customer Success" in new_s:
                self.did_escalate_cs = True
            self.update_stats()
        elif m := re.match(USER_ASSIGN_RE, hist["Description"]):
            old_s = m.group(1)
            new_s = m.group(2)
            if not self.end_assigned_user == old_s:
                print(
                    f"WARNING: Ticket #{self.ticket_nr} inconsistent history user assign action. "
                    f"Ticket user should be '{self.end_assigned_user}' but"
                    f"history says change is from '{old_s}' to '{new_s}'")
            self.end_assigned_user = new_s
            self.update_stats()
        elif m := re.match(SEVERITY_CHANGE_RE, hist["Description"]):
            old_s = m.group(1)
            new_s = m.group(2)
            if not self.end_severity_txt == old_s:
                print(
                    f"WARNING: Ticket #{self.ticket_nr} inconsistent history severity action. Ticket severity should "
                    f"be '{self.end_severity_txt}' but"
                    f"history says change is from '{old_s}' to '{new_s}'")
            self.end_severity_txt = new_s
            self.end_severity_pos = int(SEVERITY_POS_MAP.get(new_s, 0))
            self.update_stats()

    def make_next_day_record(self):
        self.update_stats()

        new_date = dateutil.parser.isoparse(self.date) + datetime.timedelta(days=1)

        r = StatisticsDayRecord(ticket_id=self.ticket_id, ticket_nr=self.ticket_nr, date=new_date.isoformat())
        r.start_status = r.end_status = self.end_status
        r.start_severity_pos = r.end_severity_pos = self.end_severity_pos
        r.start_severity_txt = r.end_severity_txt = self.end_severity_txt
        r.start_assigned_group = r.end_assigned_group = self.end_assigned_group
        r.start_assigned_user = r.end_assigned_user = self.end_assigned_user
        r.total_days_open = int(self.total_days_open) + (0 if "Closed" in self.end_status else 1)
        r.weekdays_days_since_last_contact = int(self.weekdays_days_since_last_contact) + (0 if (self.weekday in [5, 6]) else 1)  # increment the days since last contact by 1 execept for Sa and Su (i.e. if today is Fr. or Sa. then don't)

        r.adjusted_resolution_weekdays = int(self.adjusted_resolution_weekdays) + (0 if (self.end_status in STATUS_TO_EXCLUDE_FROM_RESOLUTION_TIME) or (self.weekday in [5, 6]) else 1) # increase the adjusted resolution days only if the status is open on our side and not on weekends
        r.adjusted_resolution_days = int(self.adjusted_resolution_days) + (0 if (self.end_status in STATUS_TO_EXCLUDE_FROM_RESOLUTION_TIME) else 1)  # increase the adjusted resolution days only if the status is open on our side


        return r


# Authorization token: we need to base 64 encode it
# and then decode it to acsii as python 3 stores it as a byte string
def basic_auth(username, password):
    token = base64.b64encode(f"{username}:{password}".encode('utf-8')).decode("ascii")
    return f'Basic {token}'


def get_records(act_url, header, root_element = None, sort_field='DateModified'):

    while True:
        r = requests.get(act_url, headers=header)

        if r.status_code in (200, 204):
            break
        elif r.status_code == 403: #usually this meens max exceeded
            print(f'WARNING: Probably the api request maximum is exceeded. Retry in {RETRY_INTERVAL_SECONDS} from {act_url}, error code {r.status_code}: {r.reason} -- {r.text}')
            time.sleep(RETRY_INTERVAL_SECONDS)
        else:
            print(f'ERROR: Failed to retrieve data from {act_url}, error code {r.status_code}: {r.reason} -- {r.text}')
            return None


    # some interfaces have a specific element containing all the data
    if root_element:
        d = r.json().get(root_element, [])
    else:
        d = r.json()

    print(f'{len(d)} {root_element} records retrieved from {act_url}')

    d = [__reformat_record(r) for r in d]

    cur_records = sorted(d, key=lambda x: x[sort_field])
    return cur_records

def junk_deleted_open_tickets(records, base_url, username, password, path='/api/json/Tickets'):
    """
    Check if the records that are marked as not closed via the api to see if they were not deleted.
    If a ticket was delteted in TeamSupport set its status to "Spam/Junk/delete"
    :param records: ticket recors to be verified
    :return: list of records with adjusted status
    """
    h = {
        'accept': 'application/json',
        'Authorization': basic_auth(username, password),
        'Content-Type': 'application/json',
    }
    ret = []

    for r in records:
        if r["Status"] not in ("Closed", "Spam/Junk Delete"):
            #try to get the ticket data
            act_url = urllib.parse.urljoin(base_url, f'{path}/{r["ID"]}')

            while True:
                res = requests.get(act_url, headers=h)

                if res.status_code in (200, 204):
                    ret.append(r)
                    break
                elif res.status_code == 403:  # usually this meens max exceeded
                    print(
                        f'WARNING: Probably the api request maximum is exceeded. Retry in {RETRY_INTERVAL_SECONDS} from {act_url}, error code {r.status_code}: {r.reason} -- {r.text}')
                    time.sleep(RETRY_INTERVAL_SECONDS)
                else:
                    print(
                        f'ERROR: Failed to retrieve data from {act_url}, error code {res.status_code}: {res.reason} -- {res.text}')
                    print(f'No record for ticket {r["TicketNumber"]} assuming deleted')
                    t = deepcopy(r)
                    t["Status"] = "Spam/Junk Delete"
                    t["StatusPosition"] = 10
                    t["TicketStatusID"] = "443373"
                    ret.append(t)
                    break
        else:
            ret.append(r)

    return ret


def calculate_kpi_values(tickets):
    """
    (re)calculate the value of various KPI fields in the tickets.
    Returns the tickets with recalculated KPIs (passed tickets are held unchanged)
    :param tickets: tickets to be processed
    :return: the tickets wit re-calculated KPIs
    """

    r_tickets = []

    for t_in in tickets:
        if name := t_in.get('Name'):
            excluded = False
            for r in EXCLUDE_BY_NAME_RES:
                if re.match(r, name):
                    print(f'Excluding ticket #{t_in.get("TicketNumber")} {name} due to exclude expression "{r}"')
                    excluded = True
                    break
            if excluded:
                continue
        t = t_in.copy()

        t["ActiveWeeks"] = __get_cws_active(t)
        t["CreatedWeek"] = cw_string_from_isodate(t["DateCreated"])
        if (t["Status"].find("Closed") >= 0) and len(t["History"]) > 0:
            t["ResolvedDate"] = __extract_resolve_date(t["History"])
            t["ResolvedWeek"] = cw_string_from_isodate(t["ResolvedDate"])
        else:
            t["ResolvedDate"] = None
            t["ResolvedWeek"] = None

        t["FirstResponseDate"] = __extract_first_response_date(t["Actions"])
        t["LastResponseDate"] = __extract_last_response_date(t["Actions"])
        t["Actions"] = [__reformat_record(r) for r in t["Actions"]]

        t["Calendar"] = make_ticket_calendar(t)

        #get the adjusted resolution time from the last record if the ticket is resolved
        t["AdjustedResolutionDays"] = None
        t["AdjustedResolutionWeekdays"] = None
        if (t["Status"].find("Closed") >= 0) and len(t["Calendar"]) > 0:
            t["AdjustedResolutionDays"] = t["Calendar"][-1].get('adjusted_resolution_days', None)
            t["AdjustedResolutionWeekdays"] = t["Calendar"][-1].get('adjusted_resolution_weekdays', None)


        r_tickets.append(t)

    return r_tickets


def __reformat_record(r):
    ht = html2text.HTML2Text()
    ht.ignore_links = True
    ht.ignore_images = True
    ht.ignore_mailto_links = True
    ht.ignore_emphasis = True
    ht.single_line_break = False

    add_dict_values = {}
    for k, v in r.items():
        if ("Date" in k) and ("_cw" not in k) and v:
            try:
                t = dateutil.parser.isoparse(v)
            except ValueError:
                t = dateutil.parser.parse(v, dayfirst=True)
            if not t.tzinfo:
                t = pytz.utc.localize(t)
            r[k] = t.isoformat()
            add_dict_values[k + "_cw"] = cw_string_from_isodate(r[k])
        elif ("Name" in k or "Description" in k or "Text" in k) and v:
            if not KEEP_RAW_ACTION_TEXT:
                r[k] = ht.handle(ht.handle(v[:MAX_ACTION_TEXT_LEN]))
        elif "Position" in k and v:
            r[k] = int(v)
        elif "Spent" in k and v:
            r[k] = float(v)
    r = r | add_dict_values

    if "CreatorID" in r:
        r["internal_user"] = r.get("CreatorID") in CADIS_BDK_CONTACTS
    else:
        r["internal_user"] = False

    return r


def __extract_resolve_date(history: list):
    if len(history) == 0:
        return None
    history.sort(key=lambda x: x["DateModified"], reverse=True)
    for i in history:
        if m := re.match(STATUS_CHANGE_TO_RESOLVED_RE, i["Description"]):
            return i["DateModified"]
    return None


def __extract_first_response_date(actions: list):
    if len(actions) == 0:
        return None

    actions.sort(key=lambda x: x["DateCreated"], reverse=False)

    k = 0

    for i in actions:
        # action must be public (isVisible...) and be performed by and internal contact (in CADIS_BDK_CONTACTS) and
        # must not be the very first (i.e. create) action (k>0)
        if ((i["IsVisibleOnPortal"] == "True") >= 0) \
                and (i["CreatorID"] in CADIS_BDK_CONTACTS) \
                and k > 0:
            return i["DateCreated"]
        k += 1
    return None


def __extract_last_response_date(actions: list):
    if len(actions) == 0:
        return None

    actions.sort(key=lambda x: x["DateCreated"], reverse=True)

    idx = len(actions) - 1

    for i in actions:
        # action must be public (isVisible...) and be performed by and internal contact (in CADIS_BDK_CONTACTS) and
        # must not be the very first (i.e. create) action (idx>0)
        if ((i["IsVisibleOnPortal"] == "True") >= 0) \
                and (i["CreatorID"] in CADIS_BDK_CONTACTS) \
                and idx > 0:
            return i["DateCreated"]
        idx -= 1
    return None


def cw_string_from_isodate(iso_date):
    try:
        return f'{dateutil.parser.isoparse(iso_date).year}_{dateutil.parser.isoparse(iso_date).isocalendar().week:02}'
    except Exception:
        return None


def __get_cws_active(t):
    wks = set()
    ccw = scw = cw_string_from_isodate(t["DateCreated"])
    ecw = cw_string_from_isodate(t["DateModified"])
    wks.add(scw)
    wks.add(ecw)
    d = dateutil.parser.isoparse(t["DateCreated"])
    while ccw < ecw:
        d += datetime.timedelta(days=7)
        ccw = cw_string_from_isodate(d.isoformat())
        wks.add(ccw)

    return list(wks)


def group_by_date(lst: list) -> dict:
    lst.sort(key=lambda x: x["DateCreated"])
    r = {}
    for e in lst:
        dc = e["DateCreated"][:10]
        if dc not in r:
            r[dc] = []
        r[dc].append(e)
    return r


def make_ticket_calendar(t):
    history = t["History"]
    #    history.sort(key=lambda x: x["DateCreated"])
    actions = t["Actions"]
    #    actions.sort(key=lambda x: x["DateCreated"])

    history_by_date = group_by_date(history)
    actions_by_date = group_by_date(actions)


    if len(history_by_date) > 0:
        s_date_str = min(min(history_by_date.keys()), min(actions_by_date.keys()))
    else:
        s_date_str = min(actions_by_date.keys())

    if t["Status"] in ["Closed", "Spam/Junk Delete"]:
        if len(history_by_date) > 0:
            e_date_str = max(max(history_by_date.keys()), max(actions_by_date.keys()))
        else:
            e_date_str = max(actions_by_date.keys())
    else:
        e_date_str = datetime.datetime.now(tz=pytz.utc).isoformat()[:10]

    calendar = []
    c_date = s_date_str
    sdr = StatisticsDayRecord(ticket_id=t["ID"], ticket_nr=t["TicketNumber"], date=c_date)

    while c_date <= e_date_str:
        for e in history_by_date.get(c_date, []):
            sdr.ingest_history(e)
        for e in actions_by_date.get(c_date, []):
            sdr.ingest_action(e)

        calendar.append(sdr.to_dict())

        sdr = sdr.make_next_day_record()
        c_date = dateutil.parser.isoparse(c_date) + datetime.timedelta(days=1)
        c_date = c_date.isoformat()[:10]

    return calendar


def load_tickets(base_url: str, path: str, username: str, password, filter_params=None,
                 from_ts: datetime.datetime = dateutil.parser.isoparse('2023-01-01T00:00:00.000000Z'), attachment_save_dir='./attachments') -> list | None:
    """

    :param base_url:
    :param path:
    :param username:
    :param password:
    :param filter_params:
    :param from_ts:
    :return:
    """

    #cludge for an apparent TS timzone bug - looks like they are inconsitent in which timezone they return
    from_ts -= datetime.timedelta(hours=3)

    # get all the tickets since last modified record
    if filter_params is None:
        filter_params = {}
    ticket_records = []

    h = {
        'accept': 'application/json',
        'Authorization': basic_auth(username, password),
        'Content-Type': 'application/json',
    }
    url = urllib.parse.urljoin(base_url, path)

    # start time of incremental data load. e.g. DateModified of the newest record you already have,
    # or a long past date for the initial load

    # if reverse:
    #     to_ts = from_ts - datetime.timedelta(days=LOAD_INCREMENT_IN_DAYS)
    # else:
    #     to_ts = from_ts + datetime.timedelta(days=LOAD_INCREMENT_IN_DAYS)
    to_ts = from_ts + datetime.timedelta(days=LOAD_INCREMENT_IN_DAYS)

    while from_ts <= datetime.datetime.now(tz=pytz.utc):
        dt_format = '%Y%m%d%H%M%S'
        ps = [f'{k}={v}' for k, v in filter_params.items()]
        ps += [f'DateModified[bt]={from_ts.strftime(dt_format)}', f'DateModified[bt]={to_ts.strftime(dt_format)}']
        filter_str = '&'.join(ps)
        print(f'Getting records from {url}?{filter_str} starting with timestamp {str(from_ts)} until {str(to_ts)}')

        cur_records = get_records(url + '?' + filter_str, header=h, root_element="Tickets")
        ticket_records = ticket_records + cur_records

        from_ts = to_ts
        to_ts += datetime.timedelta(days=LOAD_INCREMENT_IN_DAYS)

    ticket_records.sort(key=lambda x: x["DateModified"], reverse=True)

    # get actions and history
    n = len(ticket_records)
    i = 1
    for t in ticket_records:
        act_url = urllib.parse.urljoin(base_url, f'{path}/{t["ID"]}/History')
        t["History"] = get_records(act_url, h, 'History') or []
        print(f'Ticket #{t["TicketNumber"]} [{i}/{n}]: {len(t["History"])} History entries retrieved from {act_url}')

        act_url = urllib.parse.urljoin(base_url, f'{path}/{t["ID"]}/Actions')
        t["Actions"] = get_records(act_url, h, 'Actions') or []
        print(f'Ticket #{t["TicketNumber"]} [{i}/{n}]: {len(t["Actions"])} Action entries retrieved from {act_url}')
        if GET_ATTACHEMNTS:
            for act in t["Actions"]:
                # Create directory if it doesn't exist
                directory = Path(f"{attachment_save_dir}")
                directory.mkdir(parents=True, exist_ok=True)

                print(f'Get attachemnts {act_url}')
                act_url = urllib.parse.urljoin(base_url, f'{path}/{t["ID"]}/Actions/{act["ID"]}/Attachments')
                print(f'Get attachemnts {act_url}')
                atms = get_records(act_url, h, 'Attachments') or []
                act["Attachments"] = atms
                for atm_file in atms:
                    act_url = urllib.parse.urljoin(base_url, f'{path}/{t["ID"]}/Actions/{act["ID"]}/Attachments/{atm_file["ID"]}')
                    print(f'Get attachemnts {act_url}')

                    try:
                        response = requests.get(
                            act_url,
                            headers=h,
                            stream=True,
                        )  # Stream to avoid loading large files into memory
                        response.raise_for_status()  # Raise an error for bad responses (4xx and 5xx)

                        save_path = (
                            f"{attachment_save_dir}/#{t['TicketNumber']}_atmt_"
                            f"{atm_file['ID']}_{atm_file['FileName']}"
                        )
                        with open(save_path, 'wb') as file:
                            for chunk in response.iter_content(chunk_size=8192):
                                file.write(chunk)
                            print(f"File downloaded successfully: {save_path}")
                    except Exception as e:
                        print(
                            f"WARNING: failed to write attachment #{t['TicketNumber']}_atmt_"
                            f"{atm_file['ID']}_{atm_file['FileName']}: {str(e)}"
                        )


        t["Calendar"] = make_ticket_calendar(t)

        i += 1

    ticket_records = calculate_kpi_values(ticket_records)

    return ticket_records


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", "-u",
                        help="basic auth user name.",
                        required=True)
    parser.add_argument("--password", "-p",
                        help="basic auth password.",
                        required=True)
    parser.add_argument("--base_url", "-b", help="Base url of the cadis system, e.g. https://demo.cadis.cloud",
                        required=True)
    parser.add_argument("--data_dir", "-d",
                        help="Directory where the retrieved data will be stored. If it does not exist it will be "
                             "created. Defaults to ./cadis_data",
                        default="./cadis_data")
    parser.add_argument("--company", "-c", help="Who to get the stats for. Either Cadis or BDK are supported.",
                        required=True)
    parser.add_argument("--initial_start_date", "-s",
                        help="Initial start date from which to pull data (Starting at 00:00:00). Must be in "
                             "ISO-format (YYYY-MM-DD)."
                             "Defaults to 2023-01-01. "
                             "If there is already data present in the file system this argument is ignored. "
                             "Data appending always starts at the timestamp of the newest record.",
                        default="2023-01-01")
    parser.add_argument("--recalculate_only",
                        help="only recalculate the existing data store",
                        action='store_true')

    parser.add_argument("--keep_raw_action_text",
                        help="if set keep the raw text of action. By default the text is truncated and html is removed.",
                        action='store_true')
    parser.add_argument("--download_attachments",
                        help="if set attachments are downloaded and saved to the file system.",
                        action='store_true')


    # parser.add_argument("--reverse", "-r",
    #                     help="reverse from the oldest data entry today back to initial_start_date",
    #                     action='store_true')
    args = parser.parse_args()

    return args


def main():
    args = parse_arguments()

    global KEEP_RAW_ACTION_TEXT
    KEEP_RAW_ACTION_TEXT= args.keep_raw_action_text
    global GET_ATTACHEMNTS
    GET_ATTACHEMNTS= args.download_attachments

    # create a data store and load data hat has been retrieved and save previously
    tickets_data_store = DataStore(args.data_dir, 'tickets')

    actions_data_store = DataStore(args.data_dir, 'actions')
    history_data_store = DataStore(args.data_dir, 'history')
    calendar_data_store = DataStore(args.data_dir, 'calendar')

    # get data from thw web service starting at the timestamp of the newest record in the data store
    try:
        # if args.reverse: start_ts = tickets_data_store.oldest_record_timestamp or dateutil.parser.isoparse(f'{
        # args.initial_start_date}T00:00:00.000000Z') else: start_ts = tickets_data_store.newest_record_timestamp or
        # dateutil.parser.isoparse( f'{args.initial_start_date}T00:00:00.000000Z')
        start_ts = tickets_data_store.newest_record_timestamp or dateutil.parser.isoparse(
            f'{args.initial_start_date}T00:00:00.000000Z')
    except ValueError:
        print(
            f"Invalid date argument '{args.initial_start_date}' for --initial_start_date/-s must be in ISO format: "
            f"YYYY-MM-DD.")
        exit(2)

    if args.recalculate_only:
        records = calculate_kpi_values(tickets_data_store.data)
        actions_data_store.data = []
        history_data_store.data = []
        calendar_data_store.data = []
        tickets_data_store.data = []
    else:
        records = load_tickets(base_url=args.base_url, username=args.username, password=args.password,
                               path='/api/json/Tickets',
                               from_ts=start_ts,
                               filter_params={'GroupName[contains]': args.company},
                               attachment_save_dir = f"{args.data_dir}/attachments")

    if records:
        # add the data to the data store and save
        tickets_data_store.add_data(records)

        #recalcualte all records data and tehn replace it
        records = tickets_data_store.data
        records = junk_deleted_open_tickets(records, base_url=args.base_url, username=args.username, password=args.password)
        records = calculate_kpi_values(records)
        actions_data_store.data = []
        history_data_store.data = []
        calendar_data_store.data = []
        tickets_data_store.data = []
        tickets_data_store.add_data(records)


        # create a separate data store for actions
        actions = reduce(lambda lst, e: lst + (e["Actions"] or []), records, [])
        actions_data_store.add_data(actions)

        # create a separate data store for history
        history = reduce(lambda lst, e: lst + (e["History"] or []), records, [])
        history_data_store.add_data(history)

        # create a separate data store for ticket calendar
        calendar = reduce(lambda lst, e: lst + (e["Calendar"] or []), records, [])
        calendar_data_store.add_data(calendar)


if __name__ == '__main__':
    main()
