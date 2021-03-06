#! /bin/python
#
# --- Samsara // SFMTA integration ---
#
# Contact support@samsara.com with any issues or questions
#
#
# Notes on logging:
# - Implemented logging using Python's logging module
# - Info level messages are logged for most function entrances and exits
# - Did not implement these messages for functions that are called in the the ThreadPool
#
# Notes on exception handling
# - API requests are wrapped in try/catch blocks
# - any exception that occurs is logged as a warning
# - in the event of an exception we retry the request for MAX_RETRIES attempts
# - in the event we reach MAX_RETRIES, an error email is sent, and we log as an error
#
# Notes on error emails
# - error emails are sent via the parameters set in the following environment variables
# --- SFMTA_ERROR_FROM_EMAIL, SFMTA_ERROR_TO_EMAIL, SFMTA_ERROR_FROM_PASSWORD
# - error emails are sent at most once for every ERROR_EMAIL_DELAY
# --- this is to avoid repeated emails in the case of a persistent failure

##############################
#
#         Imports
#
##############################

import boto3
from collections import OrderedDict
from flask import Flask, render_template, request, url_for, redirect
import itertools
import json
import logging
import math
from math import radians, cos, sin, asin, sqrt, atan2
from multiprocessing.dummy import Pool as ThreadPool 
import os
import requests
import smtplib
import sys
import time
import traceback
import urllib

##############################
#
#     Config Variables
#
##############################

application = Flask(__name__)

VEHICLE_SHEETS_JSON_URL = 'https://spreadsheets.google.com/feeds/list/' + os.environ['SFMTA_VEHICLE_GOOGLE_SHEETS_KEY'] + '/od6/public/values?alt=json'
SAMSARA_LOCATIONS_URL = 'https://api.samsara.com/v1/fleet/locations?access_token=' + os.environ['SAMSARA_SFMTA_API_TOKEN']
FREQUENCY = 5				# SFMTA requires GPS ping frequency of once every 5 seconds
DISTANCE_THRESHOLD = 50		# Consider vehicle is at a SFMTA Allowed Stop if less than 50 meters away from it
MAX_RETRIES = 10			# number of times to retry an action that results in an exception
LAST_ERROR_EMAIL_TIME = 0	# initial time value to update when first error email is sent
ERROR_EMAIL_DELAY = 7200	# send an error email maximum of once every two hours
SAMSARA_SFMTA_S3 = os.environ['SAMSARA_SFMTA_S3_BUCKET']

if 'SFMTA_DEBUG' in os.environ and os.environ['SFMTA_DEBUG'] == '1':
	application.debug = True
	SFMTA_URL = 'https://stageservices.sfmta.com/shuttle/api'
else:
	SFMTA_URL = 'https://services.sfmta.com/shuttle/api'

# set the logging level
logging.basicConfig(filename = "sfmta.log", 
					level = logging.DEBUG,
					format="%(asctime)s:%(levelname)s:%(message)s")

s3 = boto3.resource('s3', region_name = 'us-west-2')

##############################
#
#     Global Variables
#
##############################

vehicle_ids = set()
placards = {}
license_plates = {}
vehicle_names = {}

vehicle_lat = {}
vehicle_long = {}
vehicle_onTrip = {}
vehicle_timestamp_ms = {}

##############################
#
#     Helper Functions
#
##############################

#Healthcheck URL for AWS
@application.route('/admin/healthcheck')
def healthcheck():
	return "Hello World!" 
# end healthcheck



# Great Circle Distance between two lat/longs
def distance(origin_lat, origin_long, dest_lat, dest_long):
	radius = 6371 * 1000 # meters

	lat1 = origin_lat
	lon1 = origin_long

	lat2 = dest_lat
	lon2 = dest_long

	if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
		return 99999

	dlat = radians(lat2-lat1)
	dlon = radians(lon2-lon1)
	a = sin(dlat/2) * sin(dlat/2) + cos(radians(lat1)) \
		* cos(radians(lat2)) * sin(dlon/2) * sin(dlon/2)
	c = 2 * atan2(sqrt(a), sqrt(1-a))
	d = radius * c

	return d
# end distance



# Gets vehicle info from a Google Sheet, and updates global variables
def get_vehicle_details(url):
	logging.info("Starting get_vehicle_details")
	try:
		response = urllib.urlopen(url)

		# there should be a function like r.raise_for_status in the urllib module
		if response.getcode() != 200:
			print 'Google Sheet returned error - ' + str(response.getcode())
			print response.read()
			print 'Continuing loop'

		else:
			json_data = json.loads(response.read())

			vehicle_ids.clear()

			for entry in json_data['feed']['entry']:
				vehicle_id = entry['gsx$samsaradeviceid']['$t']
				vehicle_ids.add(vehicle_id)

				placards[vehicle_id] = entry['gsx$vehicleplacardnumber']['$t']
				license_plates[vehicle_id] = entry['gsx$licenseplatenumber']['$t']
				vehicle_names[vehicle_id] = entry['gsx$vehicleidname']['$t']
		logging.info("Finished get_vehicle_details")
		return "Success"
	except Exception as e:
		return "Error reading vehicle details from Google Sheet\n" + str(e)
# end get_vehicle_details



# sends an email with the given body and subject
# to/from email addresses are defined as environment variables
def send_error_email(email_body, email_subject):
	logging.info("Starting send_error_email")
	formatted_lines = traceback.format_exc().splitlines()
	for j in formatted_lines:
		email_body += j
	message = 'Subject: %s\n\n%s' % (email_subject, email_body)

	from_email = os.environ['SFMTA_ERROR_FROM_EMAIL']
	to_email = os.environ['SFMTA_ERROR_TO_EMAIL']

	s = smtplib.SMTP('smtp.gmail.com', 587)
	s.ehlo() 
	s.starttls() 
	s.login(from_email, os.environ['SFMTA_ERROR_FROM_PASSWORD'])

	# Send email and close the connection
	s.sendmail(from_email, to_email, message)
	s.quit()
	logging.info("Finished send_error_email")
# end send_error_email



# Pull SFMTA Allowed Stops and store in S3
@application.route('/get_sfmta_stops', methods=['GET', 'POST'])
def get_sfmta_stops():
	headers = {'accept': 'application/json', 'content-type': 'application/json'}
	try:
		r = requests.get(SFMTA_URL+'/AllowedStops', headers = headers)
		s3.Object(SAMSARA_SFMTA_S3,'allowed_stops.json').put(Body=r.text)
		return "SFMTA Allowed Stops updated"
	except Exception as e:
		return "Error updating SFMTA Allowed Stops\n" + str(e)
# end get_sfmta_stops



# Check if a location is near one of the SFMTA AllowedStops - if yes, return the stop ID, else return 9999
def find_stop_id(stop_lat, stop_long):
	try:
		allowed_stops_object = s3.Object(SAMSARA_SFMTA_S3,'allowed_stops.json')
		allowed_stops_json = json.loads(allowed_stops_object.get()['Body'].read().decode('utf-8'))

		closest_stop_id = 9999
		min_distance = 99999

		for stop in allowed_stops_json['Stops']['Stop']:
			current_stop_id = stop['StopId']
			current_stop_lat = stop['StopLocationLatitude']
			current_stop_long = stop['StopLocationLongitude']

			curr_distance = distance(current_stop_lat,current_stop_long,stop_lat,stop_long)

			if(curr_distance < min_distance):
				min_distance = curr_distance
				closest_stop_id = current_stop_id

		if (min_distance <= DISTANCE_THRESHOLD ):
			stop_id = closest_stop_id
		else:
			stop_id = 9999

		return stop_id
	except Exception as e:
		return "Error loading data from S3 bucket\n" + str(e)
# end find_stop_id



##############################
#
#   Samsara API Functions
#
##############################

# get all vehicle telematics data from Samsara
def get_all_vehicle_data():
	logging.info("Starting get_all_vehicle_data")

	get_vehicle_details(VEHICLE_SHEETS_JSON_URL)

	group_payload = { "groupId" : int(os.environ['SAMSARA_SFMTA_GROUP_ID']) }

	# attempt to pull data from Samsara API for maximum of MAX_RETRIES attempts
	local_retries = MAX_RETRIES
	while local_retries > 0:
		try:
			r = requests.post(SAMSARA_LOCATIONS_URL, data = json.dumps(group_payload))

			# raise an exception if we get a bad HTTP response
			r.raise_for_status()

			locations_json = r.json()

			for vehicle in locations_json['vehicles']:
				vehicle_id = str(vehicle['id']).decode("utf-8")
				vehicle_lat[vehicle_id] = vehicle['latitude']
				vehicle_long[vehicle_id] = vehicle['longitude']
				vehicle_onTrip[vehicle_id] = vehicle['onTrip']

			logging.info("Finished get_all_vehicle_data")
			return "Success"

		except Exception as e:
			logging.warning('Error getting data from Samsara API -- ' + str(e))
			logging.warning('Retrying request, %s retries remaining', local_retries)
			local_retries -= 1

	# confirm that we have used MAX_RETRIES attempts
	if local_retries == 0:
		current_error_time = time.time()
		global LAST_ERROR_EMAIL_TIME

		# send error email if more than 2 hours has passed since last error email
		if (current_error_time - LAST_ERROR_EMAIL_TIME) > ERROR_EMAIL_DELAY:
			email_body = 'There was an error pulling data from Samsara API - please check logs\n\n'
			email_subject = 'Error sending data to SFMTA'
			send_error_email(email_body, email_subject)
			LAST_ERROR_EMAIL_TIME = current_error_time
			logging.error('Error email sent at ' + str(current_error_time))
		return "Failure"
# end get_all_vehicle_data



##############################
#
#    SFMTA API Functions
#
##############################

# build the payload to send to sfmta for the given vehicle and time
def build_sfmta_payload(vehicle_id, current_time):

	sfmta_payload = OrderedDict()

	sfmta_payload['TechProviderId'] = int(os.environ['SFMTA_TECH_PROVIDER_ID'])
	sfmta_payload['ShuttleCompanyId'] = os.environ['SFMTA_SHUTTLE_COMPANY_ID']
	sfmta_payload['VehiclePlacardNum'] = placards[vehicle_id]
	sfmta_payload['LicensePlateNum'] = license_plates[vehicle_id]

	if(vehicle_onTrip[vehicle_id] == True):
		vehicle_status = 1
		stop_id = 9999
	else:
		vehicle_status = 2
		stop_id = find_stop_id(vehicle_lat[vehicle_id], vehicle_long[vehicle_id])


	sfmta_payload['StopId'] = stop_id
	sfmta_payload['VehicleStatus'] = vehicle_status

	sfmta_payload['LocationLatitude'] = vehicle_lat[vehicle_id]
	sfmta_payload['LocationLongitude'] = vehicle_long[vehicle_id]
	sfmta_payload['TimeStampLocal'] = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(current_time))

	return sfmta_payload
# end build_sfmta_payload



# build payload, convert to json, then push to sfmta api
def push_vehicle_data(vehicle_id, current_time):
	sfmta_payload = build_sfmta_payload(vehicle_id, current_time)
	sfmta_payload_json = json.dumps(sfmta_payload)

	sfmta_telemetry_url = SFMTA_URL + '/Telemetry/'

	headers = {'content-type': 'application/json'}

	local_retries = MAX_RETRIES
	while local_retries > 0:
		try:
			r = requests.post(sfmta_telemetry_url, data = sfmta_payload_json, auth= (os.environ['SFMTA_USERNAME'], os.environ['SFMTA_PASSWORD']), headers = headers )
			r.raise_for_status()
			return "Success"
		except Exception as e:
			logging.warning('Error pushing data to SFMTA API -- ' + str(e))
			logging.warning('Retrying request, %s retries remaining', local_retries)
			local_retries -= 1
	# confirm that we have used MAX_RETRIES attempts
	if local_retries == 0:
		current_error_time = time.time()
		global LAST_ERROR_EMAIL_TIME

		# send error email if more than 2 hours has passed since last error email
		if (current_error_time - LAST_ERROR_EMAIL_TIME) > ERROR_EMAIL_DELAY:
			email_body = 'There was an error pushing data to SFMTA API - please check logs\n\n'
			email_subject = 'Error sending data to SFMTA'
			send_error_email(email_body, email_subject)
			LAST_ERROR_EMAIL_TIME = current_error_time
			logging.error('Error email sent at ' + str(current_error_time))
			return "Failure"
# end push_vehicle_data



# unpacks the list passed to this function to arguments, and calls function
# Convert `f([1,2])` to `f(1,2)` call
def push_vehicle_data_star(vehicle_data):
	return push_vehicle_data(*vehicle_data)
# end push_vehicle_data_star



# Push all vehicle data to SFMTA
#
# creates one thread for each vehicle, push payload for each vehicle in parallel
def push_all_vehicle_data(current_time):
	logging.info('Starting push_all_vehicle_data')

	num_vehicles = len(vehicle_ids)
	pool = ThreadPool(num_vehicles)

	logging.info('Mapping push_vehicle_data_star to each thread in the pool')
	pool.map(push_vehicle_data_star, itertools.izip(vehicle_ids,itertools.repeat(current_time)))
	logging.info('All threads finished executing push_vehicle_data')

	pool.close()
	pool.join()

	logging.info('Finished push_all_vehicle_data')
	return
# end push_all_vehicle_data



##############################
#
#   Main Application Loop
#
##############################

# Infinite loop that pulls & pushes data every 5 seconds - this is called once in a cron job at a specific time (when system is activated)
# If any errors are generated, emails are sent
@application.route('/push_sfmta')
def push_all_data():
	logging.info('Starting push_all_data')

	time.tzset()
	
	while True:
		try:
			start_time = time.time()
			current_time = start_time
			logging.info("Processing loop for time = " + str(current_time))

			# if the function fails then skip to next iteration of the loop
			if get_all_vehicle_data() != 'Success':
				continue

			samsara_api_time = time.time() - start_time
			logging.info("Samsara API time taken = "+ str(samsara_api_time))

			push_all_vehicle_data(current_time)

			end_time = time.time()
			time_spent = end_time - start_time
			logging.info("Total time taken for this iteration = " + str(time_spent))
			logging.info("Completed processing loop for time = " + str(current_time))

			time_to_wait = FREQUENCY - time_spent

			if time_to_wait >= 0:
				time.sleep(time_to_wait)

		# handle any other exceptions that may be generated
		# in the event of an exception continue to the next iteration of the loop
		except Exception as e:
			current_error_time = time.time()
			global LAST_ERROR_EMAIL_TIME

			# send error email if more than 2 hours has passed since last error email
			if (current_error_time - LAST_ERROR_EMAIL_TIME) > ERROR_EMAIL_DELAY:
				email_body = "There was an error sending data to SFMTA - please check logs\n\n"
				email_subject = "Error sending data to SFMTA"
				send_error_email(email_body, email_subject)
				LAST_ERROR_EMAIL_TIME = current_error_time
				logging.error('Error email sent at ' + str(current_error_time))
			continue
# end push_all_data
		


if __name__ == '__main__':
	if 'SFMTA_LOCALHOST' in os.environ and os.environ['SFMTA_LOCALHOST'] == '1':
		application.run(use_reloader=False)
	else:
		application.run('0.0.0.0')
