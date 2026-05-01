"""CardDAV tools for contacts management using direct HTTP/WebDAV requests."""

import sys
import requests
from requests.auth import HTTPBasicAuth
import vobject
from typing import List, Dict, Any, Optional
from fastmcp import Context
from .auth import require_auth
from .config import config
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
import uuid


# vobject's str() of an Address with only `street` populated appends
# the empty city/region/postal/country fields as separator-only junk
# (e.g. "...\n,  "). iCloud stores most addresses unstructured (everything
# in `street`), so this artifact appears on every read. Without stripping,
# read→write→read accumulates more junk on each round-trip.
_EMPTY_ADDRESS_FIELDS_RE = re.compile(r'(\n[ ,\t]*)+$')


def _address_value_str(value: Any) -> str:
    """Stringify a vobject Address, stripping vobject's trailing empty-fields artifact."""
    if value is None:
        return ""
    return _EMPTY_ADDRESS_FIELDS_RE.sub('', str(value))


def _get_carddav_session(email: str, password: str) -> tuple:
    """Create authenticated session for CardDAV (stateless)."""
    session = requests.Session()
    session.auth = HTTPBasicAuth(email, password)
    session.headers.update({
        'Content-Type': 'text/xml; charset=utf-8',
        'User-Agent': 'iCloud-MCP/1.0'
    })
    return session, email


def _copy_params(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Deep-copy a vobject params dict (values are typically lists of strings)."""
    if not params:
        return {}
    out: Dict[str, Any] = {}
    for k, v in params.items():
        out[k] = list(v) if isinstance(v, list) else v
    return out


def _apply_params(line: Any, params: Dict[str, Any]) -> None:
    """Restore previously-captured params onto a freshly-added vobject line."""
    for k, v in params.items():
        line.params[k] = list(v) if isinstance(v, list) else v


def _discover_principal(session: requests.Session, base_url: str) -> str:
    """Discover principal URL for the user."""
    propfind_body = '''<?xml version="1.0" encoding="UTF-8"?>
    <d:propfind xmlns:d="DAV:">
        <d:prop>
            <d:current-user-principal/>
        </d:prop>
    </d:propfind>'''
    
    response = session.request('PROPFIND', base_url, data=propfind_body, headers={'Depth': '0'})
    response.raise_for_status()
    
    # Parse XML response
    root = ET.fromstring(response.content)
    ns = {'d': 'DAV:'}
    principal_elem = root.find('.//d:current-user-principal/d:href', ns)
    
    if principal_elem is not None and principal_elem.text:
        return urljoin(base_url, principal_elem.text)
    
    raise ValueError("Could not discover principal URL")


def _discover_addressbook_home(session: requests.Session, principal_url: str) -> str:
    """Discover addressbook home URL."""
    propfind_body = '''<?xml version="1.0" encoding="UTF-8"?>
    <d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
        <d:prop>
            <card:addressbook-home-set/>
        </d:prop>
    </d:propfind>'''
    
    response = session.request('PROPFIND', principal_url, data=propfind_body, headers={'Depth': '0'})
    response.raise_for_status()
    
    # Parse XML response
    root = ET.fromstring(response.content)
    ns = {'d': 'DAV:', 'card': 'urn:ietf:params:xml:ns:carddav'}
    addressbook_elem = root.find('.//card:addressbook-home-set/d:href', ns)
    
    if addressbook_elem is not None and addressbook_elem.text:
        return urljoin(principal_url, addressbook_elem.text)
    
    raise ValueError("Could not discover addressbook home URL")


def _list_addressbooks(session: requests.Session, addressbook_home_url: str) -> List[Dict[str, str]]:
    """List all addressbooks."""
    propfind_body = '''<?xml version="1.0" encoding="UTF-8"?>
    <d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
        <d:prop>
            <d:displayname/>
            <d:resourcetype/>
            <card:addressbook-description/>
        </d:prop>
    </d:propfind>'''
    
    response = session.request('PROPFIND', addressbook_home_url, data=propfind_body, headers={'Depth': '1'})
    response.raise_for_status()
    
    # Parse XML response
    root = ET.fromstring(response.content)
    ns = {'d': 'DAV:', 'card': 'urn:ietf:params:xml:ns:carddav'}
    
    addressbooks = []
    for response_elem in root.findall('.//d:response', ns):
        href_elem = response_elem.find('d:href', ns)
        resourcetype_elem = response_elem.find('.//d:resourcetype', ns)
        
        # Check if this is an addressbook
        if resourcetype_elem is not None and resourcetype_elem.find('card:addressbook', ns) is not None:
            displayname_elem = response_elem.find('.//d:displayname', ns)
            
            addressbook = {
                'url': urljoin(addressbook_home_url, href_elem.text) if href_elem is not None else '',
                'name': displayname_elem.text if displayname_elem is not None and displayname_elem.text else 'Unnamed'
            }
            addressbooks.append(addressbook)
    
    return addressbooks


def _fetch_all_vcards(session: requests.Session, addressbook_url: str) -> List[Dict[str, Any]]:
    """Fetch all vCards from an addressbook."""
    # Make sure URL ends with /
    if not addressbook_url.endswith('/'):
        addressbook_url += '/'
    
    query_body = '''<?xml version="1.0" encoding="UTF-8"?>
    <card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
        <d:prop>
            <d:getetag/>
            <card:address-data/>
        </d:prop>
    </card:addressbook-query>'''
    
    try:
        response = session.request('REPORT', addressbook_url, data=query_body, headers={'Depth': '1'})
        response.raise_for_status()
    except Exception as e:
        print(f"Error fetching vCards: {str(e)}", file=sys.stderr)
        return []
    
    # Parse XML response
    vcards = []
    try:
        root = ET.fromstring(response.content)
        ns = {'d': 'DAV:', 'card': 'urn:ietf:params:xml:ns:carddav'}
        
        for response_elem in root.findall('.//d:response', ns):
            href_elem = response_elem.find('d:href', ns)
            vcard_data_elem = response_elem.find('.//card:address-data', ns)
            etag_elem = response_elem.find('.//d:getetag', ns)
            
            if vcard_data_elem is not None and vcard_data_elem.text:
                vcards.append({
                    'url': urljoin(addressbook_url, href_elem.text) if href_elem is not None else '',
                    'data': vcard_data_elem.text,
                    'etag': etag_elem.text if etag_elem is not None else ''
                })
    except Exception as e:
        print(f"Error parsing vCards: {str(e)}", file=sys.stderr)

    return vcards


async def list_contacts(
    context: Context,
    limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    List all contacts.

    Args:
        limit: Maximum number of contacts to return (optional)

    Returns:
        List of contacts with name, phone, email, address
    """
    email, password = require_auth(context)
    session, _ = _get_carddav_session(email, password)
    
    try:
        # Discover URLs
        base_url = config.CARDDAV_SERVER
        principal_url = _discover_principal(session, base_url)
        addressbook_home_url = _discover_addressbook_home(session, principal_url)
        addressbooks = _list_addressbooks(session, addressbook_home_url)
        
        if not addressbooks:
            return []
        
        # Use first addressbook
        addressbook_url = addressbooks[0]['url']
        
        # Fetch all vCards
        vcards = _fetch_all_vcards(session, addressbook_url)
        
        # Parse vCards
        result = []
        count = 0
        
        for vcard_data in vcards:
            if limit and count >= limit:
                break
            
            try:
                vcard = vobject.readOne(vcard_data['data'])
                
                contact = {
                    "id": vcard_data['url'],
                    "name": "",
                    "phones": [],
                    "emails": [],
                    "addresses": [],
                    "birthday": "",
                    "url": vcard_data['url']
                }

                # Extract name
                if hasattr(vcard, 'fn') and vcard.fn and hasattr(vcard.fn, 'value'):
                    contact["name"] = str(vcard.fn.value)

                # Extract phone numbers
                if hasattr(vcard, 'tel_list'):
                    for tel in vcard.tel_list:
                        if hasattr(tel, 'value') and tel.value:
                            contact["phones"].append(str(tel.value))

                # Extract emails
                if hasattr(vcard, 'email_list'):
                    for em in vcard.email_list:
                        if hasattr(em, 'value') and em.value:
                            contact["emails"].append(str(em.value))

                # Extract addresses
                if hasattr(vcard, 'adr_list'):
                    for adr in vcard.adr_list:
                        if hasattr(adr, 'value'):
                            try:
                                addr_str = _address_value_str(adr.value)
                                if addr_str:
                                    contact["addresses"].append(addr_str)
                            except Exception as _e:
                                continue

                # Extract birthday (vCard BDAY field, typically YYYY-MM-DD;
                # may be "--MM-DD" for partial dates without a year).
                if hasattr(vcard, 'bday') and vcard.bday and hasattr(vcard.bday, 'value'):
                    contact["birthday"] = str(vcard.bday.value)

                # Only add contact if it has a name or at least one other field
                if contact["name"] or contact["phones"] or contact["emails"]:
                    result.append(contact)
                    count += 1
            
            except Exception as e:
                print(f"Error parsing vCard: {str(e)}", file=sys.stderr)
                continue
        
        return result
    
    except Exception as e:
        raise ValueError(f"Failed to list contacts: {str(e)}")


async def get_contact(context: Context, contact_id: str) -> Dict[str, Any]:
    """
    Get a specific contact by ID.

    Args:
        contact_id: Contact URL/ID

    Returns:
        Contact details
    """
    email, password = require_auth(context)
    session, _ = _get_carddav_session(email, password)
    
    try:
        response = session.get(contact_id)
        response.raise_for_status()
        
        vcard = vobject.readOne(response.text)
        
        contact = {
            "id": contact_id,
            "name": str(vcard.fn.value) if hasattr(vcard, 'fn') else "",
            "phones": [],
            "emails": [],
            "addresses": [],
            "organization": str(vcard.org.value[0]) if hasattr(vcard, 'org') and vcard.org.value else "",
            "title": str(vcard.title.value) if hasattr(vcard, 'title') else "",
            "birthday": str(vcard.bday.value) if hasattr(vcard, 'bday') and vcard.bday else "",
            "url": contact_id
        }

        # Extract phone numbers
        if hasattr(vcard, 'tel_list'):
            for tel in vcard.tel_list:
                contact["phones"].append(str(tel.value))

        # Extract emails
        if hasattr(vcard, 'email_list'):
            for em in vcard.email_list:
                contact["emails"].append(str(em.value))

        # Extract addresses
        if hasattr(vcard, 'adr_list'):
            for adr in vcard.adr_list:
                contact["addresses"].append(_address_value_str(adr.value))

        return contact

    except Exception as e:
        raise ValueError(f"Failed to get contact: {str(e)}")


async def create_contact(
    context: Context,
    name: str,
    phones: Optional[List[str]] = None,
    emails: Optional[List[str]] = None,
    addresses: Optional[List[str]] = None,
    organization: Optional[str] = None,
    title: Optional[str] = None,
    birthday: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new contact.

    Args:
        name: Full name
        phones: List of phone numbers (optional)
        emails: List of email addresses (optional)
        addresses: List of postal addresses (optional)
        organization: Company/organization name (optional)
        title: Job title (optional)
        birthday: Birth date in vCard format — typically "YYYY-MM-DD"
            for full dates, or "--MM-DD" when the year is unknown
            (optional).

    Returns:
        Created contact details
    """
    email, password = require_auth(context)
    session, _ = _get_carddav_session(email, password)
    
    try:
        # Discover URLs
        base_url = config.CARDDAV_SERVER
        principal_url = _discover_principal(session, base_url)
        addressbook_home_url = _discover_addressbook_home(session, principal_url)
        addressbooks = _list_addressbooks(session, addressbook_home_url)
        
        if not addressbooks:
            raise ValueError("No addressbooks found")
        
        addressbook_url = addressbooks[0]['url']
        if not addressbook_url.endswith('/'):
            addressbook_url += '/'
        
        # Create vCard
        vcard = vobject.vCard()
        vcard.add('fn').value = name
        vcard.add('n').value = vobject.vcard.Name(family='', given=name)
        
        # Generate unique UID
        unique_id = str(uuid.uuid4())
        vcard.add('uid').value = unique_id
        
        # Add phones
        if phones:
            for phone in phones:
                tel = vcard.add('tel')
                tel.value = phone
                tel.type_param = 'CELL'
        
        # Add emails
        if emails:
            for em in emails:
                email_obj = vcard.add('email')
                email_obj.value = em
                email_obj.type_param = 'INTERNET'
        
        # Add addresses (strip vobject's empty-fields artifact from any
        # previously-stringified input so it doesn't accumulate)
        if addresses:
            for addr in addresses:
                adr = vcard.add('adr')
                adr.value = vobject.vcard.Address(street=_address_value_str(addr))

        # Add organization
        if organization:
            vcard.add('org').value = [organization]
        
        # Add title
        if title:
            vcard.add('title').value = title

        # Add birthday
        if birthday:
            vcard.add('bday').value = birthday

        # Serialize vCard
        vcard_data = vcard.serialize()

        # PUT vCard to server
        contact_url = f"{addressbook_url}{unique_id}.vcf"

        response = session.put(
            contact_url,
            data=vcard_data,
            headers={'Content-Type': 'text/vcard; charset=utf-8'}
        )
        response.raise_for_status()

        return {
            "id": contact_url,
            "name": name,
            "phones": phones or [],
            "emails": emails or [],
            "addresses": addresses or [],
            "organization": organization or "",
            "title": title or "",
            "birthday": birthday or "",
            "url": contact_url
        }

    except Exception as e:
        raise ValueError(f"Failed to create contact: {str(e)}")


async def update_contact(
    context: Context,
    contact_id: str,
    name: Optional[str] = None,
    phones: Optional[List[str]] = None,
    emails: Optional[List[str]] = None,
    addresses: Optional[List[str]] = None,
    organization: Optional[str] = None,
    title: Optional[str] = None,
    birthday: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update an existing contact.

    Args:
        contact_id: Contact URL/ID
        name: New full name (optional)
        phones: New list of phone numbers (optional)
        emails: New list of email addresses (optional)
        addresses: New list of postal addresses (optional)
        organization: New company/organization (optional)
        title: New job title (optional)
        birthday: New birth date — "YYYY-MM-DD" or "--MM-DD". Pass an
            empty string to remove an existing birthday; pass None
            (default) to leave it unchanged (optional).

    Returns:
        Updated contact details
    """
    email, password = require_auth(context)
    session, _ = _get_carddav_session(email, password)
    
    try:
        # Get existing vCard
        response = session.get(contact_id)
        response.raise_for_status()
        etag = response.headers.get('ETag', '')
        
        vcard = vobject.readOne(response.text)
        
        # Update fields
        if name:
            vcard.fn.value = name
        
        if phones is not None:
            # Capture each existing phone's full params dict (TYPE list,
            # 'pref' marker, X-ABLABEL, etc.) BEFORE removing so we can
            # restore them on values that round-trip unchanged. Without
            # this, callers following the read-then-write workflow would
            # clobber every phone's label set down to a flat CELL on
            # every update.
            old_phone_params: Dict[str, Dict[str, Any]] = {}
            if hasattr(vcard, 'tel_list'):
                for tel in vcard.tel_list:
                    if hasattr(tel, 'value') and tel.value is not None:
                        old_phone_params[str(tel.value)] = _copy_params(tel.params)
                for tel in list(vcard.tel_list):
                    vcard.remove(tel)
            # Re-add phones, restoring full params for round-tripped
            # values; default to TYPE=CELL for newly-added values.
            for phone in phones:
                tel = vcard.add('tel')
                tel.value = phone
                preserved = old_phone_params.get(str(phone))
                if preserved:
                    _apply_params(tel, preserved)
                else:
                    tel.type_param = 'CELL'

        if emails is not None:
            # Same params-preservation pattern as phones above (default
            # TYPE=INTERNET for newly-added values).
            old_email_params: Dict[str, Dict[str, Any]] = {}
            if hasattr(vcard, 'email_list'):
                for em in vcard.email_list:
                    if hasattr(em, 'value') and em.value is not None:
                        old_email_params[str(em.value)] = _copy_params(em.params)
                for em in list(vcard.email_list):
                    vcard.remove(em)
            for em in emails:
                email_obj = vcard.add('email')
                email_obj.value = em
                preserved = old_email_params.get(str(em))
                if preserved:
                    _apply_params(email_obj, preserved)
                else:
                    email_obj.type_param = 'INTERNET'
        
        if addresses is not None:
            # Address matching by string value isn't reliable here:
            # vobject serializes Address objects with empty city/state/zip
            # fields as separator-trailing junk (e.g. "...\n,  ") that
            # the input strings don't contain. Preserve params positionally
            # instead: the n-th new address inherits the n-th old address's
            # params (HOME / WORK / pref / etc.). Extra new entries default
            # to no params.
            old_address_params: List[Dict[str, Any]] = []
            if hasattr(vcard, 'adr_list'):
                for adr in vcard.adr_list:
                    old_address_params.append(_copy_params(adr.params))
                for adr in list(vcard.adr_list):
                    vcard.remove(adr)
            for i, addr in enumerate(addresses):
                adr = vcard.add('adr')
                adr.value = vobject.vcard.Address(street=_address_value_str(addr))
                if i < len(old_address_params) and old_address_params[i]:
                    _apply_params(adr, old_address_params[i])
        
        if organization is not None:
            if hasattr(vcard, 'org'):
                vcard.org.value = [organization]
            else:
                vcard.add('org').value = [organization]
        
        if title is not None:
            if hasattr(vcard, 'title'):
                vcard.title.value = title
            else:
                vcard.add('title').value = title

        if birthday is not None:
            # Drop the existing BDAY first so the rewrite is unambiguous,
            # then add only if a non-empty value was passed (empty string
            # is the documented "remove birthday" signal).
            if hasattr(vcard, 'bday'):
                vcard.remove(vcard.bday)
            if birthday:
                vcard.add('bday').value = birthday

        # Serialize and PUT back
        vcard_data = vcard.serialize()
        
        headers = {'Content-Type': 'text/vcard; charset=utf-8'}
        if etag:
            headers['If-Match'] = etag
        
        response = session.put(contact_id, data=vcard_data, headers=headers)
        response.raise_for_status()

        # Read every field back from the mutated vCard so the response
        # reflects the post-update state. Fields the caller didn't pass
        # are left unchanged on the vCard (the conditional blocks above
        # short-circuit when their argument is None) and the return
        # value must show that, not the input args.
        result = {
            "id": contact_id,
            "name": str(vcard.fn.value) if hasattr(vcard, 'fn') else "",
            "phones": [],
            "emails": [],
            "addresses": [],
            "organization": str(vcard.org.value[0]) if hasattr(vcard, 'org') and vcard.org.value else "",
            "title": str(vcard.title.value) if hasattr(vcard, 'title') else "",
            "birthday": str(vcard.bday.value) if hasattr(vcard, 'bday') and vcard.bday else "",
            "url": contact_id,
        }

        if hasattr(vcard, 'tel_list'):
            for tel in vcard.tel_list:
                result["phones"].append(str(tel.value))

        if hasattr(vcard, 'email_list'):
            for em in vcard.email_list:
                result["emails"].append(str(em.value))

        if hasattr(vcard, 'adr_list'):
            for adr in vcard.adr_list:
                result["addresses"].append(_address_value_str(adr.value))

        return result
    
    except Exception as e:
        raise ValueError(f"Failed to update contact: {str(e)}")


async def delete_contact(context: Context, contact_id: str) -> Dict[str, str]:
    """
    Delete a contact.

    Args:
        contact_id: Contact URL/ID to delete

    Returns:
        Confirmation message
    """
    email, password = require_auth(context)
    session, _ = _get_carddav_session(email, password)
    
    try:
        response = session.delete(contact_id)
        response.raise_for_status()
        
        return {"status": "success", "message": f"Contact {contact_id} deleted"}
    
    except Exception as e:
        raise ValueError(f"Failed to delete contact: {str(e)}")


async def search_contacts(
    context: Context,
    query: str
) -> List[Dict[str, Any]]:
    """
    Search for contacts by text query.

    Args:
        query: Search text (matches name, email, phone)

    Returns:
        List of matching contacts
    """
    # Get all contacts
    contacts = await list_contacts(context)
    
    # Filter by query
    query_lower = query.lower()
    filtered_contacts = [
        contact for contact in contacts
        if query_lower in contact.get("name", "").lower()
        or any(query_lower in email.lower() for email in contact.get("emails", []))
        or any(query_lower in phone.lower() for phone in contact.get("phones", []))
    ]
    
    return filtered_contacts
