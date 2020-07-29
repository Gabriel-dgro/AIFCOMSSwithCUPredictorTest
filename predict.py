#!/usr/bin/env python

# Modules from the Python standard library.
import datetime
import time as timelib
import math
import sys
import os
import socket
import logging
import traceback
import calendar
import optparse
import subprocess
import statsd
import tempfile
import shutil
import bisect
import simplejson as json

# handle both predict.py's
filepath = os.path.dirname(os.path.abspath(__file__))
if filepath.endswith('predict'):
    from py_variables import *
else:
    sys.path.append(os.path.join(filepath, 'predict'))
    from py_variables import *

# determine OS: darwin = Mac, other forms of win = Windows
OS_IS_WINDOWS = False
if 'win' in sys.platform.lower():
    OS_IS_WINDOWS = True

if 'darwin' in sys.platform.lower():
    OS_IS_WINDOWS = False
    
# Path to predictor binary
if OS_IS_WINDOWS:
    # Windows
    pred_binary = './pred_src/pred_StationKeep.exe'
else:
    # probably Linux or Mac
    pred_binary = './pred_src/pred_StationKeep'

statsd.init_statsd({'STATSD_BUCKET_PREFIX': 'habhub.predictor'})

# We use Pydap from http://pydap.org/.
import pydap.exceptions, pydap.client, pydap.lib
pydap.lib.CACHE = "/tmp/pydap-cache/"

# horrid, horrid monkey patching to force
# otherwise broken caching from dods server
# this is really, really hacky
# 
# import pydap.util.http
# def fresh(response_headers, request_headers):
#     cc = pydap.util.http.httplib2._parse_cache_control(response_headers)
#     if cc.has_key('no-cache'):
#         return 'STALE'
#     return 'FRESH'
# pydap.util.http.httplib2._entry_disposition = fresh

import httplib2
def fresh(response_headers, request_headers):
    cc = httplib2._parse_cache_control(response_headers)
    if cc.has_key('no-cache'):
        return 'STALE'
    return 'FRESH'
httplib2._entry_disposition = fresh

# Output logger format
log = logging.getLogger('main')
log_formatter = logging.Formatter('%(levelname)s: %(message)s')
console = logging.StreamHandler()
console.setFormatter(log_formatter)
log.addHandler(console)

progress_f = ''
progress = {
    'run_time': '',
    'gfs_percent': 0,
    'gfs_timeremaining': '',
    'gfs_complete': False,
    'gfs_timestamp': '',
    'pred_running': False,
    'pred_complete': False,
    'warnings': False,
    'pred_output': [],
    'error': '',
    }

def update_progress(**kwargs):
    global progress_f
    global progress
    for arg in kwargs:
        progress[arg] = kwargs[arg]
    try:
        progress_f.truncate(0)
        progress_f.seek(0)
        progress_f.write(json.dumps(progress))
        progress_f.flush()
        os.fsync(progress_f.fileno())
    except IOError:
        global log
        log.error('Could not update progress file')

@statsd.StatsdTimer.wrap('time')
def main():
    """
    The main program routine.
    """

    statsd.increment('run')

    # Set up our command line options
    parser = optparse.OptionParser()
    parser.add_option('-d', '--cd', dest='directory',
        help='change to, and run in, directory DIR',
        metavar='DIR')
    parser.add_option('--fork', dest='fork', action="store_true",
            help='detach the process and run in the background')
    parser.add_option('--alarm', dest='alarm', action="store_true",
            help='setup an alarm for 10 minutes time to prevent hung processes')
    parser.add_option('--redirect', dest='redirect', default='/dev/null',
            help='if forking, file to send stdout/stderr to', metavar='FILE')
    parser.add_option('-t', '--timestamp', dest='timestamp',
        help='search for dataset covering the POSIX timestamp TIME \t[default: now]', 
        metavar='TIME', type='int',
        default=calendar.timegm(datetime.datetime.utcnow().timetuple()))
    parser.add_option('-v', '--verbose', action='count', dest='verbose',
        help='be verbose. The more times this is specified the more verbose.', default=False)
    parser.add_option('-p', '--past', dest='past',
        help='window of time to save data is at most HOURS hours in past [default: %default]',
        metavar='HOURS',
        type='int', default=3)
    parser.add_option('-f', '--future', dest='future',
        help='window of time to save data is at most HOURS hours in future [default: %default]',
        metavar='HOURS',
        type='int', default=9)
    parser.add_option('--hd', dest='hd', action="store_true",
            help='use higher definition GFS data (default: no)')
    parser.add_option('--preds', dest='preds_path',
            help='path that contains uuid folders for predictions [default: %default]',
            default='./predict/preds/', metavar='PATH')
#    parser.add_option('--preds', dest='preds_path',
#            help='path that contains uuid folders for predictions [default: %default]',
#            default='./preds/', metavar='PATH')

    group = optparse.OptionGroup(parser, "Location specifiers",
        "Use these options to specify a particular tile of data to download.")
    group.add_option('--lat', dest='lat',
        help='tile centre latitude in range (-90,90) degrees north [default: %default]',
        metavar='DEGREES',
        type='float', default=52)
    group.add_option('--lon', dest='lon',
        help='tile centre longitude in degrees east [default: %default]',
        metavar='DEGREES',
        type='float', default=0)
    group.add_option('--latdelta', dest='latdelta',
        help='tile radius in latitude in degrees [default: %default]',
        metavar='DEGREES',
        type='float', default=5)
    group.add_option('--londelta', dest='londelta',
        help='tile radius in longitude in degrees [default: %default]',
        metavar='DEGREES',
        type='float', default=5)
    parser.add_option_group(group)

    #group = optparse.OptionGroup(parser, "Tile specifiers",
        #"Use these options to specify how many tiles to download.")
    #group.add_option('--lattiles', dest='lattiles',
        #metavar='TILES',
        #help='number of tiles along latitude to download [default: %default]',
        #type='int', default=1)
    #group.add_option('--lontiles', dest='lontiles',
        #metavar='TILES',
        #help='number of tiles along longitude to download [default: %default]',
        #type='int', default=1)
    #parser.add_option_group(group)

    (options, args) = parser.parse_args()

    # Check we got a UUID in the arguments
    if len(args) != 1:
        log.error('Exactly one positional argument should be supplied (uuid).')
        statsd.increment('error')
        sys.exit(1)

    if options.directory:
        os.chdir(options.directory)

    if options.fork:
        detach_process(options.redirect)

    if options.alarm:
        setup_alarm()

    uuid = args[0]
    uuid_path = options.preds_path + "/" + uuid + "/"

    # Check we're not already running with this UUID
    for line in os.popen('ps xa'):
        process = " ".join(line.split()[4:])
        if process.find(uuid) > 0:
            pid = int(line.split()[0])
            if pid != os.getpid():
                statsd.increment('duplicate')
                log.error('A process is already running for this UUID, quitting.')
                sys.exit(1)

    # Make the UUID directory if non existant
    if not os.path.exists(uuid_path):
        os.mkdir(uuid_path, 0o770)

    # Open the progress.json file for writing, creating it and closing again to flush
    global progress_f
    global progress
    try:
        progress_f = open(uuid_path+"progress.json", "w+")
        update_progress(
            gfs_percent=0,
            gfs_timeremaining="Please wait...",
            run_time=str(int(timelib.time())))
    except IOError:
        log.error('Error opening progress.json file')
        statsd.increment('error')
        sys.exit(1)
    
    # Check the predictor binary exists
    if not os.path.exists(pred_binary):
        log.error('Predictor binary does not exist.')
        statsd.increment('error')
        sys.exit(1)

    # Check the latitude is in the right range.
    if (options.lat < -90) | (options.lat > 90):
        log.error('Latitude %s is outside of the range (-90,90).')
        statsd.increment('error')
        sys.exit(1)

    # Check the delta sizes are valid.
    if (options.latdelta <= 0.5) | (options.londelta <= 0.5):
        log.error('Latitiude and longitude deltas must be at least 0.5 degrees.')
        statsd.increment('error')
        sys.exit(1)

    if options.londelta > 180:
        log.error('Longitude window sizes greater than 180 degrees are meaningless.')
        statsd.increment('error')
        sys.exit(1)

    # We need to wrap the longitude into the right range.
    options.lon = canonicalise_longitude(options.lon)

    # How verbose are we being?
    if options.verbose > 0:
        log.setLevel(logging.INFO)
    if options.verbose > 1:
        log.setLevel(logging.DEBUG)
    if options.verbose > 2:
        logging.basicConfig(level=logging.INFO)
    if options.verbose > 3:
        logging.basicConfig(level=logging.DEBUG)

    log.debug('Using cache directory: %s' % pydap.lib.CACHE)

    timestamp_to_find = options.timestamp
    time_to_find = datetime.datetime.utcfromtimestamp(timestamp_to_find)
    # utcoffset = datetime.timedelta(hours = 7.0)
    # time_to_find -= utcoffset

    log.info('Looking for latest dataset which covers %s' % time_to_find.ctime())
    try:
        dataset = dataset_for_time(time_to_find, options.hd)
    except:
        log.error('Could not locate a dataset for the requested time.')
        statsd.increment('no_dataset')
        statsd.increment('error')
        sys.exit(1)

#    dataset_times = map(timestamp_to_datetime, dataset.time)
#    dataset_timestamps = map(datetime_to_posix, dataset_times)
    dataset_times = list(map(timestamp_to_datetime, dataset.time))
    dataset_timestamps = list(map(datetime_to_posix, dataset_times))

    log.info('Found appropriate dataset:')
    log.info('    Start time: %s (POSIX %s)' % \
        (dataset_times[0].ctime(), dataset_timestamps[0]))
    log.info('      End time: %s (POSIX %s)' % \
        (dataset_times[-1].ctime(), dataset_timestamps[-1]))

    # print('dataset.lat = ', dataset.lat)
    # print('dataset[lat][:] = ', dataset['lat'][:])
    # print('list(dataset.lat) = ', list(dataset.lat))
    # print('dataset[lat][:].data = ', dataset['lat'][:].data)

    # log.info('      Latitude: %s -> %s' % (min(dataset.lat), max(dataset.lat)))
    # log.info('     Longitude: %s -> %s' % (min(dataset.lon), max(dataset.lon)))
    log.info('      Latitude: %s -> %s' % (min(list(dataset.lat)), max(list(dataset.lat))))
    log.info('     Longitude: %s -> %s' % (min(list(dataset.lon)), max(list(dataset.lon))))

#    for dlat in range(0,options.lattiles):
#        for dlon in range(0,options.lontiles):
    window = ( \
            options.lat, options.latdelta, \
            options.lon, options.londelta)

#    gfs_dir = "/var/www/cusf-standalone-predictor/gfs/"
    gfs_dir = os.path.join(ROOT_DIR, "gfs")

    gfs_dir = tempfile.mkdtemp(dir=gfs_dir)

    gfs_filename = "gfs_%(time)_%(lat)_%(lon)_%(latdelta)_%(londelta).dat"
    output_format = os.path.join(gfs_dir, gfs_filename)

    write_file(output_format, dataset, \
            window, \
            time_to_find - datetime.timedelta(hours=options.past), \
            time_to_find + datetime.timedelta(hours=options.future))

    #purge_cache()
    
    update_progress(gfs_percent=100, gfs_timeremaining='Done', gfs_complete=True, pred_running=True)
    
    if options.alarm:
        alarm_flags = ["-a120"]
    else:
        alarm_flags = []

    command = [pred_binary, '-i', gfs_dir, '-vv', '-o', uuid_path+'flight_path.csv', uuid_path+'scenario.ini']
    log.info('The command is:')
    log.info(command)
    pred_process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    pred_output = []

    while True:
        line = pred_process.stdout.readline()
        if line == b'':
            break

        # pass through
        # sys.stdout.write(line)
        # the required Python 3 obfuscation of the above line ...
        sys.stdout.write(line.decode(sys.stdout.encoding))

        # if "ERROR: Do not have wind data" in line:
        # more required Python 3 obfuscation ...
        if b'ERROR: Do not have wind data' in line:
            pred_output = ["One of the latitude, longitude or time deltas ({0}, {1}, {2}) was too small."
                           .format(options.latdelta, options.londelta, options.future),
                           "Please adjust the settings accordingly and re-run your prediction.",
                           ""] + pred_output

        # if ("WARN" in line or "ERROR" in line) and len(pred_output) < 10:
        # more required Python 3 obfuscation ...
        if (b'WARN' in line or b'ERROR' in line) and len(pred_output) < 10:
            pred_output.append(line.strip())

    exit_code = pred_process.wait()
    
    if OS_IS_WINDOWS:
        copy_path = os.path.join(ROOT_DIR, "predict")
    else:
        copy_path = '/tmp'            
            
    if exit_code == 1:
        # Hard error from the predictor. Tell the javascript it completed, so that it will show the trace,
        # but pop up a 'warnings' window with the error messages
        update_progress(pred_running=False, pred_complete=True, warnings=True, pred_output=pred_output)
        statsd.increment('success_serious_warnings')
    elif pred_output:
        # Soft error (altitude too low error, typically): pred_output being set forces the debug
        # window open with the messages in
        update_progress(pred_running=False, pred_complete=True, pred_output=pred_output)
        statsd.increment('success_minor_warnings')
    else:
        log.info('The predictor pred.exe executable exit_code = %s' % exit_code )
        assert exit_code == 0
        update_progress(pred_running=False, pred_complete=True)
        statsd.increment('success')  
 
    copy_path = os.path.join(copy_path, 'flight_path.csv')

    log.info('Copying file:')
    log.info(uuid_path+'flight_path.csv')
    log.info('to file:')
    log.info(copy_path)

    shutil.copyfile(uuid_path+'flight_path.csv',copy_path)

    shutil.rmtree(gfs_dir)



def purge_cache():
    """
    Purge the pydap cache (if set).
    """

    if pydap.lib.CACHE is None:
        return

    log.info('Purging PyDAP cache.')

    for file in os.listdir(pydap.lib.CACHE):
        log.debug('   Deleting %s.' % file)
        os.remove(pydap.lib.CACHE + file)

def write_file(output_format, thedata, window, mintime, maxtime):
    log.info('Downloading data in window (lat, lon) = (%s +/- %s, %s +/- %s).' % window)

    # Firstly, get the hgtprs variable to extract the times we're going to use.
    hgtprs_global = thedata['hgtprs']

    # Check the dimensions are what we expect.
    assert(hgtprs_global.dimensions == ('time', 'lev', 'lat', 'lon'))

    # Work out what times we want to download
    times = sorted(map(timestamp_to_datetime, hgtprs_global.maps['time']))
    times_first = max(0, bisect.bisect_right(times, mintime) - 1)
    times_last = min(len(times), bisect.bisect_left(times, maxtime) + 1)
    times = times[times_first:times_last]

    num_times = len(times)
    current_time = 0

    start_time = min(times)
    end_time = max(times)
    log.info('Downloading from %s to %s.' % (start_time.ctime(), end_time.ctime()))

    # print('enumerate(hgtprs_global.maps[lon]) = ', enumerate(hgtprs_global.maps['lon']))    
    # print('list(enumerate(hgtprs_global.maps[lon])) = ', list(enumerate(hgtprs_global.maps['lon'])))    

    # Filter the longitudes we're actually going to use.
    # longitudes = filter(lambda x: longitude_distance(x[1], window[2]) <= window[3] ,
    #                     enumerate(hgtprs_global.maps['lon']))
    # Below is the Python 3 version of the above.
    # (As the saying goes -- How many computer scientists does it take to make things worse?  Answer: All of them.)
    longitudes = []
    for count,ele in enumerate(hgtprs_global.maps['lon']):
        if longitude_distance(ele, window[2]) <= window[3]:
            longitudes.append([count,ele])

    # Filter the latitudes we're actually going to use.
    # latitudes = filter(lambda x: math.fabs(x[1] - window[0]) <= window[1] ,
    #                    enumerate(hgtprs_global.maps['lat']))
    # Below is the (again, worse) Python 3 version of the above.
    latitudes = []
    for count,ele in enumerate(hgtprs_global.maps['lat']):
        if math.fabs(ele - window[0]) <= window[1]:
            latitudes.append([count,ele])

    update_progress(gfs_percent=10, gfs_timeremaining="Please wait...")

    starttime = datetime.datetime.now()

    # Write one file for each time index.
    # for timeidx, time in enumerate(hgtprs_global.maps['time']):
    for timeidx, time in list(enumerate(hgtprs_global.maps['time'])):

        timestamp = datetime_to_posix(timestamp_to_datetime(time))
        if (timestamp < datetime_to_posix(start_time)) | (timestamp > datetime_to_posix(end_time)):
            continue

        current_time += 1
        
        log.info('Downloading data for %s.' % (timestamp_to_datetime(time).ctime()))

        downloaded_data = { }
        current_var = 0
        time_per_var = datetime.timedelta()
        for var in ('hgtprs', 'ugrdprs', 'vgrdprs', 'tmpprs', 'vvelprs'):
            current_var += 1
            #k grid = thedata['hgtprs.hgtprs']
            grid = thedata[var]
            log.info('Processing variable \'%s\' with shape %s...' % (var, grid.shape))   #j

            # Check the dimensions are what we expect.
            assert(grid.dimensions == ('time', 'lev', 'lat', 'lon'))

            # print('longitudes[0][0] = ', longitudes[0][0])
            # print('longitudes[-1][0] = ', longitudes[-1][0])
            # print('latitudes[0][0] = ', latitudes[0][0])
            # print('latitudes[-1][0] = ', latitudes[-1][0])
            # print('timeidx = ', timeidx)
            ## print('hgtprs_global.maps[lat].shape[0]-1 = ', hgtprs_global.maps['lat'].shape[0]-1)
            # print('grid = ', grid)
            # print('thedata = ', thedata)
            # print('grid[time] = ', grid['time'])
            # print('grid[time].data = ', grid['time'].data)
            ## print('grid.array[:] = ', grid.array[:])
            ## print('grid.array[:].data = ', grid.array[:].data)
            ## print('grid.array = ', grid.array)
            ## print('grid.array.data = ', grid.array.data)
            ## print('grid[:] = ', grid[:])
            # print('list(grid) = ', list(grid))
            ## print('grid[var] = ', grid[var])
            # print('grid.dimensions = ', grid.dimensions)
            # print('grid.maps = ', grid.maps)
            # print('grid.time = ', grid.time)
            ## print('grid.shape = ', grid.shape)
            # print('grid.attributes = ', grid.attributes)
            # print('grid.time.shape = ', grid.time.shape)
            # print('grid.lev.shape = ',  grid.lev.shape)
            # print('grid.lat.shape = ',  grid.lat.shape)
            # print('grid.lon.shape = ',  grid.lon.shape)
            ## testsela = thedata[1,10:14,5:10,5:10]
            # testsela = grid[2,10:14,5:10,5:10]
            ## print('testsela.shape = ', testsela.shape)
            ## print('data[var][:] = ', data[var][:])
            ## testsela = list(data)
            ## testselb = testsela[var]
            ## print('list(grid.var) = ', list(grid.var))
            ## print('selection on grid[time] = ', grid['time'])

            # See if the longitude region wraps...
            if (longitudes[0][0] == 0) & (longitudes[-1][0] == hgtprs_global.maps['lat'].shape[0]-1):
                # Download the data. Unfortunately, OpeNDAP only supports remote access of
                # contiguous regions. Since the longitude data wraps, we may require two 
                # windows. The 'right' way to fix this is to download a 'left' and 'right' window
                # and munge them together. In terms of download speed, however, the overhead of 
                # downloading is so great that it is quicker to download all the longitude 
                # data in our slice and do the munging afterwards.
                selection = grid[\
                    timeidx,
                    :, \
                    latitudes[0][0]:(latitudes[-1][0]+1), \
                    : ]
            else:
                # selection = grid[2, :, 0:181, 0:360] # for testing
                selection = grid[\
                    timeidx,
                    :, \
                    latitudes[0][0]:(latitudes[-1][0]+1), \
                    longitudes[0][0]:(longitudes[-1][0]+1) ]

            # Cache the downloaded data for later
            downloaded_data[var] = selection

            log.info('   Downloaded data has shape %s...', selection.shape)  #j
            # assert len(selection.shape) == 3                               #j
            assert len(selection.shape) == 4                                 #j

            now = datetime.datetime.now()
            time_elapsed = now - starttime
            num_vars = (current_time - 1)*5 + current_var
            time_per_var = time_elapsed / num_vars
            total_time = num_times * 5 * time_per_var
            time_left = total_time - time_elapsed
            time_left = timelib.strftime('%M:%S', timelib.gmtime(time_left.seconds))
            
            update_progress(gfs_percent=int(
                10 +
                (((current_time - 1) * 90) / num_times) +
                ((current_var * 90) / (5 * num_times))
                ), gfs_timeremaining=time_left)

        # Check all the downloaded data has the same shape
        target_shape = downloaded_data['hgtprs']
        #j assert( all( map( lambda x: x == target_shape,                      
        #j        filter( lambda x: x.shape, iter(downloaded_data.values()) ) ) ) ) 

        log.info('Writing output...')

        hgtprs = downloaded_data['hgtprs']
        ugrdprs = downloaded_data['ugrdprs']
        vgrdprs = downloaded_data['vgrdprs']
        tmpprs = downloaded_data['tmpprs']
        vvelprs = downloaded_data['vvelprs']

        # log.debug('Using longitudes: %s' % (map(lambda x: x[1], longitudes),))
        # update the above for Python 3
        log.debug('Using longitudes: %s to %s' % (longitudes[0][0], (longitudes[-1][0]+1)))

        output_filename = output_format
        output_filename = output_filename.replace('%(time)', str(timestamp))
        output_filename = output_filename.replace('%(lat)', str(window[0]))
        output_filename = output_filename.replace('%(latdelta)', str(window[1]))
        output_filename = output_filename.replace('%(lon)', str(window[2]))
        output_filename = output_filename.replace('%(londelta)', str(window[3]))

        log.info('   Writing \'%s\'...' % output_filename)
        output = open(output_filename, 'w')

        # Write the header.
        output.write('# window centre latitude, window latitude radius, window centre longitude, window longitude radius, POSIX timestamp\n')
        header = window + (timestamp,)
        output.write(','.join(map(str,header)) + '\n')

        # Write the axis count.
        output.write('# num_axes\n')
        output.write('3\n') # FIXME: HARDCODED!

        # Write each axis, a record showing the size and then one with the values.
        output.write('# axis 1: pressures\n')
        output.write(str(hgtprs.maps['lev'].shape[0]) + '\n')
        output.write(','.join(map(str,hgtprs.maps['lev'][:])) + '\n')
        output.write('# axis 2: latitudes\n')
        output.write(str(len(latitudes)) + '\n')
        output.write(','.join(map(lambda x: str(x[1]), latitudes)) + '\n')
        output.write('# axis 3: longitudes\n')
        output.write(str(len(longitudes)) + '\n')
        output.write(','.join(map(lambda x: str(x[1]), longitudes)) + '\n')

        # Write the number of lines of data.
        output.write('# number of lines of data\n')                                  #j
        output.write('%s\n' % (hgtprs.shape[1] * len(latitudes) * len(longitudes)))  #j

        # Write the number of components in each data line.
        output.write('# data line component count\n')
        output.write('5\n') # FIXME: HARDCODED!

        # Write the data itself.
        output.write('# now the data in axis 3 major order\n')
        output.write('# data is: '
                     'geopotential height [gpm], u-component wind [m/s], '
                     'v-component wind [m/s], temperature [K], '
                     'vertical velocity (pressure) [Pa/s]\n')
        for pressureidx, pressure in enumerate(hgtprs.maps['lev']):
            for latidx, latitude in enumerate(hgtprs.maps['lat']):
                for lonidx, longitude in enumerate(hgtprs.maps['lon']):
                    if longitude_distance(longitude, window[2]) > window[3]:
                        continue
                    # record = ( hgtprs.array[pressureidx,latidx,lonidx], \
                    #            ugrdprs.array[pressureidx,latidx,lonidx], \
                    #            vgrdprs.array[pressureidx,latidx,lonidx], \
                    #            tmpprs.array[pressureidx,latidx,lonidx], \
                    #            vvelprs.array[pressureidx,latidx,lonidx] )
                    record = ( hgtprs.array[0,pressureidx,latidx,lonidx].data, \
                               ugrdprs.array[0,pressureidx,latidx,lonidx].data, \
                               vgrdprs.array[0,pressureidx,latidx,lonidx].data, \
                               tmpprs.array[0,pressureidx,latidx,lonidx].data, \
                               vvelprs.array[0,pressureidx,latidx,lonidx].data )
                    output.write(','.join(map(str,record)) + '\n')

def canonicalise_longitude(lon):
    """
    The GFS model has all longitudes in the range 0.0 -> 359.5. Canonicalise
    a longitude so that it fits in this range and return it.
    """
    lon = math.fmod(lon, 360)
    if lon < 0.0:
        lon += 360.0
    assert((lon >= 0.0) & (lon < 360.0))
    return lon

def longitude_distance(lona, lonb):
    """
    Return the shortest distance in degrees between longitudes lona and lonb.
    """
    distances = ( \
        math.fabs(lona - lonb),  # Straightforward distance
        360 - math.fabs(lona - lonb), # Other way 'round.
    )
    return min(distances)

def datetime_to_posix(time):
    """
    Convert a datetime object to a POSIX timestamp.
    """
    return calendar.timegm(time.timetuple())

def timestamp_to_datetime(timestamp):
    """
    Convert a GFS fractional timestamp into a datetime object representing 
    that time.
    """
    # The GFS timestmp is a floating point number fo days from the epoch,
    # day '0' appears to be January 1st 1 AD.

    (fractional_day, integer_day) = math.modf(timestamp)

    # Unfortunately, the datetime module uses a different epoch.
    ordinal_day = int(integer_day - 1)

    # Convert the integer day to a time and add the fractional day.
    return datetime.datetime.fromordinal(ordinal_day) + \
        datetime.timedelta(days = fractional_day)

def possible_urls(time, hd):
    """
    Given a datetime object representing a date and time, return a list of
    possible data URLs which could cover that period.

    The list is ordered from latest URL (i.e. most likely to be correct) to
    earliest.

    We assume that a particular data set covers a period of P days and
    hence the earliest data set corresponds to time T - P and the latest
    available corresponds to time T given target time T.
    """

    # print('start possible_urls at time =', time)
    period = datetime.timedelta(days = 7.5)
    # nomads dataset available times are screwed up online
    utcoffset = datetime.timedelta(hours = 7.0)
    # utcoffset = datetime.timedelta(hours = 15.0)

    earliest = time - period
    # latest = time
    latest = time - utcoffset
    # print('latest =', latest)

    # nomads.ncep.noaa.gov now uses https rather than http (began Feb 2019):
    if hd:
        url_format = 'https://{host}:9090/dods/gfs_0p25/gfs%i%02i%02i/gfs_0p25_%02iz'
    #    url_format = 'http://{host}:9090/dods/gfs_0p25/gfs%i%02i%02i/gfs_0p25_%02iz'
    #    url_format = 'http://{host}:9090/dods/gfs_hd/gfs_hd%i%02i%02i/gfs_hd_%02iz'
    else:
        url_format = 'https://{host}:9090/dods/gfs_1p00/gfs%i%02i%02i/gfs_1p00_%02iz'
    #    url_format = 'http://{host}:9090/dods/gfs_1p00/gfs%i%02i%02i/gfs_1p00_%02iz'
    #    url_format = 'http://{host}:9090/dods/gfs/gfs%i%02i%02i/gfs_%02iz'

    # Often we have issues where one IP address (the DNS resolves to 2 or more)
    # will have a dataset and the other one won't yet.
    # This causes "blah is not an available dataset" errors since predict.py
    # thinks it's OK to use a recent one, and then by chance we end up talking
    # to a server on a later request that doesn't have it.
    # print('url_format =', url_format)
    selected_ip = socket.gethostbyname("nomads.ncep.noaa.gov")
    log.info("Picked IP: {0}".format(selected_ip))
    # url_format = url_format.format(host=selected_ip)
    url_format = url_format.format(host='nomads.ncep.noaa.gov')
    # print('url_format including host =', url_format)

    # Start from the latest, work to the earliest
    proposed = latest
    possible_urls = []
    while proposed >= earliest:
        for offset in ( 18, 12, 6, 0 ):
            if proposed.day != latest.day or proposed.hour >= offset:
                possible_urls.append(url_format % \
                    (proposed.year, proposed.month, proposed.day, offset))
                print('.....................', offset, url_format % (proposed.year, proposed.month, proposed.day, offset))
        proposed -= datetime.timedelta(days = 1)
        print('.....................', proposed)
    
    print('possible_urls returns: ', possible_urls)

    return possible_urls

def dataset_for_time(time, hd):
    """
    Given a datetime object, attempt to find the latest dataset which covers that 
    time and return pydap dataset object for it.
    """

    print('start dataset_for_time at time =', time)
    url_list = possible_urls(time, hd)
    print('the dataset_for_time url_list = ', url_list)

    for url in url_list:
        try:
            log.debug('Trying dataset at %s.' % url)
            print('Trying dataset at : ', url)
            from pydap.client import open_url
            dataset = open_url(url)
            # dataset = open_url(url, output_grid=False)
            print('dataset_for_time dataset returned by pydap = ', dataset)
            # print('with time = ', dataset.time)
            # print('and location = ', dataset.location)
            # print('and another time = ', dataset['time'][:])
            # print('and start time = ', dataset['time'][0])
            # print('and end time = ', dataset['time'][-1])
            ## print('and start time data = ', dataset['time'][0].data)
            ## print('and end time data = ', dataset['time'][-1].data)
            # print('and sub-time = ', dataset.time.data)

            # start_time = timestamp_to_datetime(dataset.time[0])
            # end_time = timestamp_to_datetime(dataset.time[-1])
            start_time = timestamp_to_datetime(dataset['time'][0].data)  #j
            end_time = timestamp_to_datetime(dataset['time'][-1].data)   #j

            print('start time = ', start_time)
            print('end time = ', end_time)
            print('time = ', time)
            if start_time <= time and end_time >= time:
                log.info('Found good dataset at %s.' % url)
                dataset_id = url.split("/")[5] + "_" + url.split("/")[6].split("_")[1]
                update_progress(gfs_timestamp=dataset_id)
                return dataset
#        except:
#            raise Exception()
        except pydap.exceptions.ServerError as e:
            log.debug('Server error in dataset at %s from %s' % (url, e) )
            # Skip server error.
            pass

    print('RuntimeError of Could not find appropriate dataset.')
    raise RuntimeError('Could not find appropriate dataset.')

def detach_process(redirect):
    # Fork
    if os.fork() > 0:
        os._exit(0)

    # Detach
    os.setsid()

    null_fd = os.open(os.devnull, os.O_RDONLY)
    out_fd = os.open(redirect, os.O_WRONLY | os.O_APPEND)

    os.dup2(null_fd, sys.stdin.fileno())
    for s in [sys.stdout, sys.stderr]:
        os.dup2(out_fd, s.fileno())

    # Fork
    if os.fork() > 0:
        os._exit(0)

def alarm_workaround(parent):
    # wait for the parent
    parent.join(600)
    # if the parent (main) thread is still alive, then we need to kill it
    if parent.isAlive():
        os._exit(0)

def setup_alarm():
    # Prevent hung download:
    if OS_IS_WINDOWS:
        import threading
        t = threading.Thread(target=alarm_workaround, args=(threading.currentThread(),))
        # setting the thread as a daemon means we don't need to worry about cleaning it up
        t.daemon = True
        t.start()
    else:
        import signal
        signal.alarm(600)

# If this is being run from the interpreter, run the main function.
if __name__ == '__main__':
    try:
        main()
    except SystemExit as e:
        log.debug("Exit: " + repr(e))
        if e.code != 0 and progress_f:
            update_progress(error="Unknown error exit")
            statsd.increment("unknown_error_exit")
        raise
    except Exception as e:
        statsd.increment("uncaught_exception")
        log.exception("Uncaught exception")
        info = traceback.format_exc()
        if progress_f:
            update_progress(error="Unhandled exception: " + info)
        raise
