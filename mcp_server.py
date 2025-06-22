#!/usr/bin/env python3
"""
Kurtosis MCP Server

A Model Context Protocol server for interfacing with KU Leuven's KURT reservation system.
Provides tools to list study spaces, query availability, and generate booking/check-in links.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(name="KU Leuven Reservation Tool")


# Constants
KURT_API_BASE_URL = "https://wsrt.ghum.kuleuven.be/service1.asmx"
BOOKING_BASE_URL = (
    "https://www-sso.groupware.kuleuven.be/sites/KURT/Pages/NEW-Reservation.aspx"
)
CHECKIN_BASE_URL = "https://kurt3.ghum.kuleuven.be/check-in/"


async def load_studyspaces_data() -> list[dict]:
    """Load studyspaces data from the remote JSON endpoint"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://kurtosis.breitburg.com/studyspaces.json"
            )
            response.raise_for_status()
            return response.json()
    except Exception:
        return []


@mcp.tool()
async def list_study_spaces() -> str:
    """List all available KU Leuven study spaces with their details"""
    studyspaces_data = await load_studyspaces_data()

    if not studyspaces_data:
        return "Error: Could not load study spaces data. Please try again or visit https://kuleuven.be/kurt to browse study spaces manually."

    output_lines = []
    output_lines.append("Available KU Leuven Study Spaces:\\n")

    for space in studyspaces_data:
        building_name = space.get("buildingName", "Unknown Building")
        space_name = space.get("spaceName", "Unknown Space")
        seats = space.get("seats", {})
        seat_count = len(seats)

        # Generate unique seat name patterns by replacing numbers with XX
        seat_patterns = set()
        for seat_name in seats.values():
            # Replace any sequence of digits with XX
            pattern = re.sub(r"\d+", "XX", seat_name)
            seat_patterns.add(pattern)

        patterns_str = ", ".join(sorted(seat_patterns))

        output_lines.append(f"{building_name} - {space_name}")
        output_lines.append(f"   • {seat_count} seats available: {patterns_str}")
        output_lines.append(f"   • Location ID: {space.get('locationId', 'N/A')}")
        output_lines.append("")

    return "\\n".join(output_lines)


@mcp.tool()
async def query_availability(
    building_name: str,
    space_name: str,
    date: str,
    availability_regex: str,
    userId: str,
    seat_name_regex: str | None = None,
) -> str:
    """Query seat availability for a specific study space on a given date and time range

    Args:
        building_name: Name of the building (e.g. 'Agora Learning Center')
        space_name: Name of the study space (e.g. 'Silent Study')
        date: Date in YYYY-MM-DD format (e.g. '2025-06-21')
        availability_regex: Regex pattern to filter by availability slots. Examples: '1[2-4]' (lunch hours 12-14), '2[0-3]' (evening 20-23), '\\b[8-9]\\b|1[01]' (morning 8-11), '1[5-9]' (afternoon 15-19), '([0-9]+,){3,}' (4+ available hours)
        userId: KU Leuven user ID (R-number, U-number, or B-number)
        seat_name_regex: Optional regex pattern to filter seats by name. Examples: '^Silent.*' (starts with Silent), '.*WNDW.*' (contains WNDW), 'Group Study.*' (group study seats), '.*1[0-2]X' (seats 10X-12X range)
    """
    # Validate date range (up to 8 days ahead)
    try:
        query_date = datetime.strptime(date, "%Y-%m-%d").date()
        today = datetime.now().date()
        max_date = today + timedelta(days=8)

        if query_date < today:
            return f"Error: Cannot query past dates. Please select today or a future date up to 8 days ahead. Try again or visit https://kuleuven.be/kurt to check availability manually."

        if query_date > max_date:
            return f"Error: Date is too far in the future. You can only book up to 8 days ahead (until {max_date.strftime('%Y-%m-%d')}). Try again with an earlier date or visit https://kuleuven.be/kurt to check availability manually."

    except ValueError:
        return f"Error: Invalid date format. Please use YYYY-MM-DD format. Try again or visit https://kuleuven.be/kurt to check availability manually."

    studyspaces_data = await load_studyspaces_data()

    # Find the matching study space
    target_space = None
    for space in studyspaces_data:
        if (
            space.get("buildingName", "").lower() == building_name.lower()
            and space.get("spaceName", "").lower() == space_name.lower()
        ):
            target_space = space
            break

    if not target_space:
        return f"Error: Could not find study space '{space_name}' in '{building_name}'. Please try again with correct names or visit https://kuleuven.be/kurt to browse available spaces."

    seats = target_space.get("seats", {})
    if not seats:
        return f"Error: No seats found for {building_name} - {space_name}. Please try again or visit https://kuleuven.be/kurt to check availability manually."

    # Compile regex patterns
    name_pattern = None

    try:
        if seat_name_regex:
            name_pattern = re.compile(seat_name_regex)
        availability_pattern = re.compile(availability_regex)
    except re.error as e:
        return f"Error: Invalid regex pattern - {str(e)}. Please try again with a valid regex or visit https://kuleuven.be/kurt to search manually."

    # Filter seats by name regex if provided
    filtered_seats = {}
    total_seats = len(seats)

    for resource_id, seat_name in seats.items():
        if name_pattern is None or name_pattern.search(seat_name):
            filtered_seats[resource_id] = seat_name

    if not filtered_seats:
        return f"Error: No seats match the name pattern '{seat_name_regex}'. Please try again with a different pattern or visit https://kuleuven.be/kurt to browse seats manually."

    # Query availability from KURT API for filtered seats
    resource_ids = list(filtered_seats.keys())
    try:
        availability_data = await fetch_kurt_availability(resource_ids, date, userId)
    except Exception as e:
        return f"Error querying availability: {str(e)}. Please try again or visit https://kuleuven.be/kurt to check availability manually."

    # Process and humanize the results
    output_lines = []
    output_lines.append(f"Availability for {building_name} - {space_name} on {date}:")

    # Add filtering information
    filters_applied = [f"availability: '{availability_regex}'"]
    if seat_name_regex:
        filters_applied.append(f"seat name: '{seat_name_regex}'")
    output_lines.append(f"Filters applied: {', '.join(filters_applied)}")

    if seat_name_regex:
        output_lines.append(
            f"Seats filtered: {len(filtered_seats)}/{total_seats} seats match name pattern"
        )

    output_lines.append("")

    # Group availability by seat (check all hours 8-23)
    seat_availability = {}
    for resource_id, seat_name in filtered_seats.items():
        available_slots = []

        # Check each hour in the full day range (8-23)
        for hour in range(8, 24):
            slot_key = f"{resource_id}-{hour}"
            if slot_key not in availability_data.get("busy_slots", set()):
                available_slots.append(hour)

        if available_slots:
            # Apply availability regex filter to comma-separated string for better matching
            slots_str = ",".join(map(str, available_slots))
            if availability_pattern.search(slots_str):
                seat_availability[seat_name] = {
                    "resource_id": resource_id,
                    "available_slots": available_slots,
                }

    if not seat_availability:
        output_lines.append("No seats available that match the availability pattern")
    else:
        matching_seats = len(seat_availability)
        output_lines.append(
            f"Available seats matching pattern ({matching_seats} found):"
        )

        for seat_name, info in seat_availability.items():
            resource_id = info["resource_id"]
            available_slots = info["available_slots"]
            output_lines.append(f"{seat_name} ({resource_id}): {available_slots}")

    return "\\n".join(output_lines)


@mcp.tool()
async def get_booking_link(
    resource_id: str, date: str, start_hour: int, end_hour: int
) -> str:
    """Generate a booking link for a specific seat/resource

    Args:
        resource_id: The resource/seat ID (e.g. '300855')
        date: Date in YYYY-MM-DD format
        start_hour: Start hour (24-hour format)
        end_hour: End hour (24-hour format)
    """
    try:
        date_obj = datetime.strptime(date, "%Y-%m-%d")

        # Format start and end datetimes
        start_datetime = date_obj.replace(hour=start_hour, minute=0, second=0)
        end_datetime = date_obj.replace(hour=end_hour, minute=0, second=0)

        # Handle end time crossing midnight
        if end_hour <= start_hour:
            end_datetime += timedelta(days=1)

        start_str = start_datetime.strftime("%Y-%m-%dT%H:%M:%S")
        end_str = end_datetime.strftime("%Y-%m-%dT%H:%M:%S")

        params = {
            "StartDateTime": start_str,
            "EndDateTime": end_str,
            "ID": resource_id,
            "type": "b",
        }

        booking_url = f"{BOOKING_BASE_URL}?" + urlencode(params)

        return f"Booking link for resource {resource_id}:\\n{booking_url}\\n\\nTime: {start_datetime.strftime('%Y-%m-%d %H:%M')} - {end_datetime.strftime('%H:%M')}"

    except Exception as e:
        return f"Error generating booking link: {str(e)}. Please try again or visit https://kuleuven.be/kurt to book manually."


@mcp.tool()
async def get_checkin_link(resource_id: str) -> str:
    """Generate a check-in link for a specific seat/resource

    Args:
        resource_id: The resource/seat ID (e.g. '300855')
    """
    try:

        checkin_url = f"{CHECKIN_BASE_URL}{resource_id}"

        return f"Check-in link for resource {resource_id}:\\n{checkin_url}"

    except Exception as e:
        return f"Error generating check-in link: {str(e)}. Please try again or visit https://kuleuven.be/kurt to check in manually."


async def fetch_kurt_availability(resource_ids: list[str], date: str, uid: str) -> dict:
    """Fetch availability data from KURT API"""
    # Format dates for API
    date_obj = datetime.strptime(date, "%Y-%m-%d")
    start_datetime = date_obj.strftime("%Y-%m-%dT00:00:00")
    next_day = date_obj + timedelta(days=1)
    end_datetime = next_day.strftime("%Y-%m-%dT00:00:00")

    # Build API URL
    params = {
        "uid": uid,
        "ResourceIDList": ",".join(resource_ids),
        "startdtstring": start_datetime,
        "enddtstring": end_datetime,
    }

    url = f"{KURT_API_BASE_URL}/GetReservationsJSON?" + urlencode(params)

    # Make API request
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()

    # Process response to identify busy slots
    busy_slots = set()
    for item in data:
        resource_id = item.get("ResourceID")
        start_datetime_str = item.get("Startdatetime")
        status = item.get("Status")

        if resource_id and start_datetime_str and status and status != "Available":
            try:
                start_dt = datetime.fromisoformat(
                    start_datetime_str.replace("Z", "+00:00")
                )
                hour = start_dt.hour
                slot_key = f"{resource_id}-{hour}"
                busy_slots.add(slot_key)
            except Exception:
                pass

    return {"busy_slots": busy_slots}


if __name__ == "__main__":
    mcp.run()
