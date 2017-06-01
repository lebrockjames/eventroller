import datetime
import re

from actionkit.api.event import AKEventAPI
from actionkit.api.user import AKUserAPI
from event_store.models import Activist, Event, CHOICES

"""
Non-standard use in ActionKit:
* We assume a user field called "recent_phone" (because the phone table is a big pain)
* Custom Event Field mappings:
  - review_status
  - prep_status
  - needs_organizer_help
  - political_scope
  - public_phone
  - venue_category

"""

#MYSQL 2016-12-12 18:00:00
DATE_FMT = '%Y-%m-%d %H:%M:%S'

class AKAPI(AKUserAPI, AKEventAPI):
    #merge both user and event apis in one class
    pass

class Connector:
    """
    This connects to ActionKit with the rest api -- queries are done through
    ad-hoc report queries: https://roboticdogs.actionkit.com/docs/manual/api/rest/reports.html#running-an-ad-hoc-query
    which is inelegant compared with browsing /rest/v1/event/ however, we can't get all the fields
    we need from that one call, and it's currently impossible to sort by updated_at for easy syncing
    and it's very difficult to get the hosts without browsing all signups.  Better would be a
    way to filter eventsignups by role=host
    """

    description = ("ActionKit API connector that needs API- read-only access and API edit access"
                   " if you are going to save event status back")

    CAMPAIGNS_CACHE = {}
    USER_CACHE = {}

    #used for conversions
    date_fields = ('starts_at', 'ends_at', 'starts_at_utc', 'ends_at_utc', 'updated_at')

    common_fields = ['address1', 'address2',
                     'city', 'state', 'region', 'postal', 'zip', 'plus4', 'country',
                     'longitude', 'latitude',
                     'title', 'starts_at', 'ends_at', 'starts_at_utc', 'ends_at_utc', 'status', 'host_is_confirmed',
                     'is_private', 'is_approved', 'attendee_count', 'max_attendees',
                     'venue',
                     'public_description', 'directions', 'note_to_attendees', 'notes',
                     'updated_at']

    other_fields = ['ee.id', 'ee.creator_id', 'ee.campaign_id', 'ee.phone',
                    'ec.title', 'signuppage.name',
                    'u.id', 'u.first_name', 'u.last_name', 'u.email', 'loc.us_district', 'recentphone.value']

    event_fields = ['review_status', 'prep_status',
                    'needs_organizer_help', 'political_scope', 'public_phone', 'venue_category']

    #column indexes for the above fields
    field_indexes = {k:i for i,k in enumerate(common_fields + other_fields + event_fields)}

    sql_query = (
        "SELECT %(commonfields)s, %(otherfields)s, %(eventfields)s"
        " FROM events_event ee"
        " JOIN events_campaign ec ON ee.campaign_id = ec.id"
        #host won't necessarily be unique but the GROUP BY will choose the first host signup
        " LEFT JOIN events_eventsignup host ON (host.event_id = ee.id AND host.role='host'"
        "                                        AND host.user_id NOT IN {{ excludes }} )"
        " LEFT JOIN core_user u ON (u.id = host.user_id)"
        " LEFT JOIN core_userfield recentphone ON (recentphone.parent_id = u.id AND recentphone.name = 'recent_phone')"
        " LEFT JOIN core_location loc ON (loc.user_id = u.id)"
        " JOIN core_eventsignuppage ces ON (ces.campaign_id = ec.id)"
        " JOIN core_page signuppage ON (signuppage.id = ces.page_ptr_id AND signuppage.hidden=0)"
        " %(eventjoins)s "
        " xxADDITIONAL_WHERExx " #will be replaced with text or empty string on run
        " GROUP BY ee.id" #make sure events are unique (might arbitrarily choose signup page, if multiple)
        " ORDER BY {{ ordering }} DESC"
        " LIMIT {{ max_results }}"
        " OFFSET {{ offset }}"
    ) % {'commonfields': ','.join(['ee.{}'.format(f) for f in common_fields]),
         'otherfields': ','.join(other_fields),
         'eventfields': ','.join(['{f}.value'.format(f=f) for f in event_fields]),
         'eventjoins': ' '.join([("LEFT JOIN events_eventfield {f}"
                                  " ON ({f}.parent_id=ee.id AND {f}.name = '{f}')"
                              ).format(f=f) for f in event_fields]),
                                   }


    @classmethod
    def writable(cls):
        return True

    @classmethod
    def parameters(cls):
        return {'campaign': {'help_text': 'ID (a number) of campaign if just for a single campaign',
                             'required': False},
                'api_password': {'help_text': 'api password',
                            'required': True},
                'api_user': {'help_text': 'api username',
                             'required': True},
                'max_event_load': {'help_text': 'The default number of events to back-load from the database.  (if not set, then it will go all the way back)',
                                   'required': False},
                'base_url': {'help_text': 'base url like "https://roboticdocs.actionkit.com"',
                                  'required': True},
                'ak_secret': {'help_text': 'actionkit "Secret" needed for auto-login tokens',
                              'required': False},
                'ignore_host_ids': {'help_text': ('if you want to ignore certain hosts'
                                                  ' (due to automation/admin status) add'
                                                  ' them as a json list of integers'),
                                    'required': False}
        }

    def __init__(self, event_source):
        self.source = event_source
        data = event_source.data

        self.base_url = data['base_url']
        class aksettings:
            AK_BASEURL = data['base_url']
            AK_USER = data['api_user']
            AK_PASSWORD = data['api_password']
            AK_SECRET = data.get('ak_secret')
        self.akapi = AKAPI(aksettings)
        self.ignore_hosts = data['ignore_host_ids'] if 'ignore_host_ids' in data else []

    def _load_events_from_sql(self, ordering='ee.updated_at', max_results=10000, offset=0,
                    excludes=[], additional_where=[], additional_params={}):
        """
        With appropriate sql query gets all the events via report/run/sql api
        and returns None when there's an error or no events and returns
        a list of event row lists with column indexes described by self.field_indexes
        """
        if max_results > 10000:
            raise Exception("ActionKit doesn't permit adhoc sql queries > 10000 results")
        if not excludes:
            excludes = [0] #must have at least one value
        where_clause = ''
        if additional_where:
            where_clause = ' WHERE %s' % ' AND '.join(additional_where)
        query = {'query': self.sql_query.replace('xxADDITIONAL_WHERExx', where_clause),
                 'ordering': ordering,
                 'max_results': max_results,
                 'offset': offset,
                 'excludes': excludes}
        query.update(additional_params)
        res = self.akapi.client.post('{}/rest/v1/report/run/sql/'.format(self.base_url),
                                     json=query)
        if res.status_code == 200:
            return res.json()

    def _convert_host(self, event_row):
        fi = self.field_indexes
        return Activist(member_system=self.source,
                        member_system_pk=str(event_row[fi['u.id']]),
                        name='{} {}'.format(event_row[fi['u.first_name']], event_row[fi['u.last_name']]),
                        email=event_row[fi['u.email']],
                        hashed_email=Activist.hash(event_row[fi['u.email']]),
                        phone=event_row[fi['recentphone.value']])

    def _convert_event(self, event_row):
        """
        Based on a row from self.sql_query, returns a
        dict of fields that correspond directly to an event_store.models.Event object
        """
        fi = self.field_indexes
        event_fields = {k:event_row[fi[k]] for k in self.common_fields}
        signuppage = event_row[fi['signuppage.name']]
        e_id = event_row[fi['ee.id']]
        rsvp_url = (
            '{base}/event/{attend_page}/{event_id}/'.format(
                base=self.base_url, attend_page=signuppage, event_id=e_id)
            if signuppage else None)
        search_url = (
            '{base}/event/{attend_page}/search/'.format(
                base=self.base_url, attend_page=signuppage)
            if signuppage else None)
        slug = '{}-{}'.format(re.sub(r'\W', '', self.base_url.split('://')[1]), e_id)
        state, district = (event_row[fi['loc.us_district']] or '_').split('_')
        ocdep_location = ('ocd-division/country:us/state:{}/cd:{}'.format(state.lower(), district)
                          if state and district else None)

        event_fields.update({'organization_official_event': False,
                             'event_type': 'unknown',
                             'organization_host': self._convert_host(event_row),
                             'organization_source': self.source,
                             'organization_source_pk': str(e_id),
                             'organization': self.source.origin_organization,
                             'organization_campaign': event_row[fi['ec.title']],
                             'is_searchable': (event_row[fi['status']] == 'active'
                                               and not event_row[fi['is_private']]),
                             'private_phone': event_row[fi['recentphone.value']] or '',
                             'phone': event_row[fi['public_phone']] or '',
                             'url': rsvp_url, #could also link to search page with hash
                             'slug': slug,
                             'osdi_origin_system': self.base_url,
                             'ticket_type': CHOICES['open'],
                             'share_url': search_url,
                             #e.g. NC cong district 2 = "ocd-division/country:us/state:nc/cd:2"
                             'political_scope': (event_row[fi['political_scope']] or ocdep_location),
                             #'dupe_id': None, #no need to set it
                             'venue_category': CHOICES[event_row[fi['venue_category']] or 'unknown'],
                             #TODO: if host_ids are only hosts, then yes, but we need a better way to filter role=host signups
                             'needs_organizer_help': event_row[fi['needs_organizer_help']] == 'needs_organizer_help',
                             'rsvp_url': rsvp_url,
                             'event_facebook_url': None,
                             'organization_status_review': event_row[fi['review_status']],
                             'organization_status_prep': event_row[fi['prep_status']],
                         })
        for df in self.date_fields:
            if event_fields[df]:
                event_fields[df] = datetime.datetime.strptime(event_fields[df], DATE_FMT)
        return event_fields

    def get_event(self, event_id):
        """
        Returns an a dict with all event_store.Event model fields
        """
        excludes = self.source.data.get('excludes')
        events = self._load_events_from_sql(excludes=excludes,
                                            additional_where=['ee.id = {{event_id}}'],
                                            additional_params={'event_id': event_id})
        if events:
            return self._convert_event(events[0])

    def load_events(self, max_events=None, last_updated=None):
        additional_where = []
        additional_params = {}
        campaign = self.source.data.get('campaign')
        excludes = self.source.data.get('excludes')
        if campaign:
            additional_where.append('ee.campaign_id = {{ campaign_id }}')
            additional_params['campaign_id'] = campaign
        if last_updated:
            additional_where.append('ee.updated_at > {{ last_updated }}')
            additional_params['last_updated'] = last_updated
        all_events = []
        max_events = max_events or self.source.data.get('max_event_load')
        event_count = 0
        for offset in range(0, max_events, min(10000, max_events)):
            if event_count > max_events:
                break
            events = self._load_events_from_sql(offset=offset, excludes=excludes,
                                                additional_where=additional_where,
                                                additional_params=additional_params,
                                                max_results=min(10000, max_events))
            if events:
                for event_row in events:
                    event_count = event_count + 1
                    all_events.append(self._convert_event(event_row))
        return {'events': all_events,
                'last_updated': datetime.datetime.utcnow().strftime(DATE_FMT)}

    def update_review(self, review):
        pass

    def get_host_event_link(self, event):
        #might include a temporary token
        pass
