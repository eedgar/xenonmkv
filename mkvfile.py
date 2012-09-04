import subprocess
import fractions
import os
import sys

from reference_frame import ReferenceFrameValidator
from mkv_info_parser import MKVInfoParser
from process_handler import ProcessHandler
from track import *

class MKVFile():
	path = ""
	tracks = {}
	duration = 0

	video_track_id = audio_track_id = 0
	
	log = args = None

	def __init__(self, path, log, args):
		self.path = path
		self.log = log
		self.args = args
	
	def get_path(self):
		return self.path

	# Open the mkvinfo process and parse its output.
	def get_mkvinfo(self):
		track_count = 0
		self.log.debug("Executing 'mkvinfo %s'" % self.get_path())
		try: 
			result = subprocess.check_output([self.args.tool_paths["mkvinfo"], self.get_path()])
		except subprocess.CalledProcessError as e:
			self.log.debug("mkvinfo process error: " + e.output)
			raise Exception("Error occurred while obtaining MKV information for %s - please make sure the file exists, is readable and a valid MKV file" % self.get_path())

		self.log.debug("mkvinfo finished; attempting to parse output")
		try:
			self.parse_mkvinfo(result)
		except:
			# Punt back exception from inner function to the main application
			raise
		
	# Open the mediainfo process to obtain detailed info on the file.		
	def get_mediainfo(self, track_type):
		if track_type == "video":
			parameters = "Video;%ID%,%Height%,%Width%,%Format_Settings_RefFrames%,%Language%,%FrameRate%,%CodecID%,%DisplayAspectRatio%,~"
		else:
			parameters = "Audio;%ID%,%CodecID%,%Language%,%Channels%,~"
			
		self.log.debug("Executing 'mediainfo %s %s'" % (parameters, self.get_path()))
		result = subprocess.check_output([self.args.tool_paths["mediainfo"], "--Inform=" + parameters, self.get_path()])
		self.log.debug("mediainfo finished; attempting to parse output for %s settings" % track_type)
		return self.parse_mediainfo(result)		

	def parse_mediainfo(self, result):
		output = []
		result = result.replace("\n", "")
		
		# Obtain multiple tracks if they are present
		lines = result.split("~")
		lines = lines[0:-1] # remove last tilde separator character
		
		for line in lines:
			# remove last \n element from array that will always be present
			values = line.split(",")
			# print values
			output.append(values)

		return output

	# Return a float value specifying the display aspect ratio.
	def parse_display_aspect_ratio(self, dar_string):
		self.log.debug("Attempting to parse display aspect ratio '%s'" % dar_string)
		if "16/9" in dar_string:
			return 1.778
		elif "4/3" in dar_string:
			return 1.333
		elif "/" in dar_string:
			# Halfass some math and try to get an approximate number.
			try: 
				numerator = int(dar_string[0:dar_string.index("/")])
				denominator = int(dar_string[dar_string.index("/") + 1:])
				return numerator / denominator
			except:
				# Couldn't divide
				raise Exception("Could not parse display aspect ratio of %s" % dar_string)
		else:
			return float(dar_string.strip())
			 
	# Calculate the pixel aspect ratio of the track based on the height, width, and display A/R
	def calc_pixel_aspect_ratio(self, track):
		t_height = track.height * track.display_ar
		t_width = track.width
		gcd = fractions.gcd(t_height, t_width)
		self.log.debug("GCD of %i height, %i width is %i" % (t_height, t_width, gcd))

		if gcd == 0:
			# Pixel aspect ratio should be 1:1
			t_height = 1
			t_width = 1
			gcd = 1
		else:
			# We can do division on integers here because the denominator is common
			t_height = t_height / gcd
			t_width = t_width / gcd
		
		# If height and width are extraordinarily large, bring them down by a multiple of 10 simultaneously
		while t_height > 1000 or t_width > 1000:
			t_height = t_height / 10
			t_width = t_width / 10

		self.log.debug("Calculated pixel aspect ratio is %i:%i (%f)" % (t_height, t_width, t_height / t_width))
		
		if not self.args.no_round_par:
			if t_height / t_width > 0.98 and t_height / t_width < 1:
				self.log.debug("Rounding pixel aspect ratio up to 1:1")
				t_height = t_width = 1
			elif t_height / t_width < 1.01 and t_height / t_width > 1:
				self.log.debug("Rounding pixel aspect ratio down to 1:1")
				t_height = t_width = 1
		
		return str(t_height) + ":" + str(t_width)
		
	# Return the fixed duration of the MKV file.
	def get_duration(self):
		return self.duration

	# Parse the 'duration' line in the mkvinfo output to estimate a duration for the file.
	def parse_audio_duration(self, output):
		audio_int = 0

		duration_detect_string = "| + Duration: "
		audio_duration = output[output.index(duration_detect_string) + len(duration_detect_string):]

		if not "s" in audio_duration:
			raise Exception("Could not parse MKV duration - no 's' specified")

		audio_duration = audio_duration[0:audio_duration.index("s")]

		self.log.debug("Audio duration detected as %s seconds" % audio_duration)

		# Check if there is a decimal value; if so, add one second
		if "." in audio_duration:
			audio_duration = audio_duration[0:audio_duration.index(".")]
			audio_int += 1
	
		audio_int += int(audio_duration)
		self.log.debug("Audio duration for MKV file may have been rounded: using %i seconds" % audio_int)

		return audio_int

	# Parse the output from mkvinfo for the file.
	def parse_mkvinfo(self, result):
		track_detect_string = "| + A track"
		
		if not track_detect_string in result:
			raise Exception("mkvinfo: output did not contain any tracks")

		track_info = result
		self.duration = self.parse_audio_duration(track_info)

		# Multiple track support:
		# Extract mediainfo profile for all tracks in file, then cross-reference them
		# with the output from mkvinfo. This prevents running mediainfo multiple times.

		mediainfo_video_output = self.get_mediainfo("video")
		mediainfo_audio_output = self.get_mediainfo("audio")

		# For ease of use, throw these values into a dictionary with the key being the track ID.
		mediainfo = {}

		for mediainfo_track in mediainfo_video_output:
			mediainfo[int(mediainfo_track[0])] = mediainfo_track[1:]
		for mediainfo_track in mediainfo_audio_output:
			mediainfo[int(mediainfo_track[0])] = mediainfo_track[1:]

		# Create a new parser that can be used for all tracks
		info_parser = MKVInfoParser(self.log)
		has_audio = has_video = False

		while track_detect_string in track_info:
			track = MKVTrack(self.log)
			if track_detect_string in track_info:
				track_info = track_info[track_info.index(track_detect_string) + len(track_detect_string):]
			else:
				break

			# Get track type and number out of this block
			track_type = info_parser.parse_track_type(track_info)
			track_number = info_parser.parse_track_number(track_info)

			# Set individual track properties for the object by track ID
			if track_type in ("video", "audio"):
				mediainfo_track = mediainfo[track_number]

			if track_type == "video":
				has_video = True
				track = VideoTrack(self.log)
				track.number = track_number
				track.default = info_parser.parse_track_is_default(track_info)

				track.height = int(mediainfo_track[0])
				track.width = int(mediainfo_track[1])
				
				try:
					# Possible condition: no reference frames detected
					# If so, just set to zero and log a debug message				
					track.reference_frames = int(mediainfo_track[2])
				except ValueError:
					track.reference_frames = 0
					self.log.debug("Reference frame value '%s' in track %i could not be parsed; assuming 0 reference frames" % (mediainfo_track[2], track.number))
					
				track.language = mediainfo_track[3]
				track.frame_rate = float(mediainfo_track[4])
				track.codec_id = mediainfo_track[5]
				track.display_ar = self.parse_display_aspect_ratio(mediainfo_track[6])
				track.pixel_ar = self.calc_pixel_aspect_ratio(track)

				self.log.debug("Video track %i has dimensions %ix%i with %i reference frames" % (track.number, track.width, track.height, track.reference_frames))
				self.log.debug("Video track %i has %f FPS and codec %s" % (track.number, track.frame_rate, track.codec_id))
				self.log.debug("Video track %i has display aspect ratio %f" % (track.number, track.display_ar))

				if self.reference_frames_exceeded(track):
					self.log.warning("Video track %i contains too many reference frames to play properly on low-powered devices" % track.number)
					if not self.args.ignore_reference_frames:
						raise Exception("Video track %i has too many reference frames")
				else:
					self.log.debug("Video track %i has a reasonable number of reference frames, and should be compatible with low-powered devices" % track.number)

	
			elif track_type == "audio":
				has_audio = True
				track = AudioTrack(self.log)
				track.number = track_number
				track.default = info_parser.parse_track_is_default(track_info)

				track.codec_id = mediainfo_track[0]
				track.language = mediainfo_track[1]
				track.channels = int(mediainfo_track[2])

				# Indicate if the audio track needs a recode. By default, it does.
				# Check that the audio type is AAC, and if the number of channels in the file
				# is less than or equal to what was specified on the command line, no recode is necessary
				if track.codec_id == "A_AAC":
					# Check on the number of channels in the file versus the argument passed.
					if track.channels <= args.channels:
						self.log.debug("Audio track %i will not need to be re-encoded (%s channels specified, %i channels in file)" % (track.number, args.channels, track.channels))
						track.needs_recode = False

				self.log.debug("Audio track %i has codec %s and language %s" % (track.number, track.codec_id, track.language))
				self.log.debug("Audio track %i has %i channel(s)" % (track.number, track.channels))
				
			else:
				# Unrecognized track type. Don't completely abort processing, but do log it.
				# Do not proceed to add this to the global tracks list.
				self.log.debug("Unrecognized track type '%s' in %i; skipping" % (track_type, track_number)) 
				continue

			self.log.debug("All properties set for %s track %i" % (track_type, track.number))
			track.track_type = track_type
			self.tracks[track.number] = track

		# All tracks detected here
		self.log.debug("All tracks detected from mkvinfo output; total number is %i" % len(self.tracks))
		
		# Make sure that there is at least one audio and one video track and throw an exception if not
		if not has_video:
			raise Exception("No video track found in MKV file %s" % self.path)
		elif not has_audio:
			raise Exception("No audio track found in MKV file %s" % self.path)

	def reference_frames_exceeded(self, video_track):
		return ReferenceFrameValidator.validate(video_track.height, video_track.width, video_track.reference_frames)

	def has_multiple_av_tracks(self):
		video_tracks = audio_tracks = 0
		for track_id in self.tracks:
			track = self.tracks[track_id]
			if track.track_type == "video":
				video_tracks += 1
			elif track.track_type == "audio":
				audio_tracks += 1

		return (video_tracks > 1 or audio_tracks > 1)
		
	def set_video_track(self, track_id):
		if self.tracks[track_id] and self.tracks[track_id].track_type == "video":
			self.video_track_id = track_id
		else:
			raise Exception("Video track with ID %i was not found in file" % track_id)
	
	def set_audio_track(self, track_id):
		if self.tracks[track_id] and self.tracks[track_id].track_type == "audio":
			self.audio_track_id = track_id
		else:
			raise Exception("Audio track with ID %i was not found in file" % track_id)

	def set_default_av_tracks(self):
		for track_id in self.tracks:
			track = self.tracks[track_id]
			if track.track_type == "video" and track.default and not self.video_track_id:
				self.video_track_id = track.number
			elif track.track_type == "audio" and track.default and not self.audio_track_id:
				self.audio_track_id = track.number
				
		# Check again if we have tracks specified here. If not, nothing was hinted as default
		# in the MKV file and we should really pick the first audio and first video track.
		if not self.video_track_id:
			self.log.debug("No default video track was specified in '%s'; using first available" % self.path)
			for track_id in self.tracks:
				track = self.tracks[track_id]
				if track.track_type == "video":
					self.video_track_id = track.number
					log.debug("First available video track in file is %i" % self.video_track_id)
					break
					
		if not self.audio_track_id:
			self.log.debug("No default audio track was specified in '%s'; using first available" % self.path)
			for track_id in self.tracks:
				track = self.tracks[track_id]
				if track.track_type == "audio":
					self.audio_track_id = track.number
					log.debug("First available audio track in file is %i" % self.audio_track_id)
					break
					
		# If we still don't have a video and audio track specified, it's time to throw an error.
		if not self.video_track_id or not self.audio_track_id:
			raise Exception("Could not select an audio and video track for MKV file '%s'" % self.path)

	def get_audio_track(self):
		return self.tracks[self.audio_track_id]

	def get_video_track(self):
		return self.tracks[self.video_track_id]

	def extract_mkv(self):
		self.log.debug("Executing mkvextract on %s'" % self.get_path())
		prev_dir = os.getcwd()

		if self.args.scratch_dir != ".":
			self.log.debug("Using %s as scratch directory for MKV extraction" % self.args.scratch_dir)
			os.chdir(self.args.scratch_dir)
			
		self.log.debug("Using video track from MKV file with ID %i" % self.video_track_id)
		self.log.debug("Using audio track from MKV file with ID %i" % self.audio_track_id)

		try:
			temp_video_file = "temp_video" + self.tracks[self.video_track_id].get_filename_extension()
			temp_audio_file = "temp_audio" + self.tracks[self.audio_track_id].get_filename_extension()
		except UnsupportedCodecError:
			# Send back to main application
			raise

		if self.args.resume_previous and os.path.isfile(temp_video_file) and os.path.isfile(temp_audio_file):
			self.log.debug("Temporary video and audio files already exist; cancelling extract")
			temp_video_file = os.path.join(os.getcwd(), temp_video_file)
			temp_audio_file = os.path.join(os.getcwd(), temp_audio_file)
			os.chdir(prev_dir)
			return (temp_video_file, temp_audio_file)
	
		# Remove any existing files with the same names
		if os.path.isfile(temp_video_file):
			self.log.debug("Deleting temporary video file %s" % os.path.join(os.getcwd(), temp_video_file))
			os.unlink(temp_video_file)

		if os.path.isfile(temp_audio_file):
			self.log.debug("Deleting temporary audio file %s" % os.path.join(os.getcwd(), temp_audio_file))
			os.unlink(temp_audio_file)

		video_output = str(self.video_track_id) + ":" + temp_video_file
		audio_output = str(self.audio_track_id) + ":" + temp_audio_file

		cmd = [self.args.tool_paths["mkvextract"], "tracks", self.get_path(), video_output, audio_output]
		ph = ProcessHandler(self.args)
		process = ph.start_process(cmd)

		while True:
			out = process.stdout.read(1)
			if out == '' and process.poll() != None:
				break
			if out != '' and not self.args.quiet:
				sys.stdout.write(out)
				sys.stdout.flush()

		if process.returncode != 0:
			raise Exception("An error occurred while extracting tracks from %s - please make sure this file exists and is readable" % self.get_path())

		temp_video_file = os.path.join(os.getcwd(), temp_video_file)
		temp_audio_file = os.path.join(os.getcwd(), temp_audio_file)

		os.chdir(prev_dir)
		self.log.debug("mkvextract finished; attempting to parse output")
		
		if not temp_video_file or not temp_audio_file:
			raise Exception("Audio or video file missing from mkvextract output")
		
		return (temp_video_file, temp_audio_file)