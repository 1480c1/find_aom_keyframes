import sys
import struct
import os

# This is a script that returns a list of keyframes that aom would likely place. Port of aom's C code.
# It requires an aom first-pass stats file as input. FFMPEG first-pass file is not OK. Default filename is stats.bin.
# Script has been tested to have ~99% accuracy vs final aom encode.

# Elements related to parsing the stats file were written by MrSmilingWolf

# Copyright (c) 2020 motbob
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# Fields meanings: <source root>/av1/encoder/firstpass.h
fields = ['frame', 'weight', 'intra_error', 'frame_avg_wavelet_energy', 'coded_error', 'sr_coded_error', 'tr_coded_error'
         ,'pcnt_inter', 'pcnt_motion', 'pcnt_second_ref', 'pcnt_third_ref', 'pcnt_neutral', 'intra_skip_pct', 'inactive_zone_rows'
         ,'inactive_zone_cols', 'MVr', 'mvr_abs', 'MVc', 'mvc_abs', 'MVrv', 'MVcv', 'mv_in_out_count', 'new_mv_count', 'duration', 'count', 'raw_error_stdev']

def get_second_ref_usage_thresh(frame_count_so_far):
    adapt_upto = 32
    min_second_ref_usage_thresh = 0.085
    second_ref_usage_thresh_max_delta = 0.035
    if frame_count_so_far >= adapt_upto:
        return min_second_ref_usage_thresh + second_ref_usage_thresh_max_delta
    return min_second_ref_usage_thresh + (frame_count_so_far / (adapt_upto - 1)) * second_ref_usage_thresh_max_delta

#I have no idea if the following function is necessary in the python implementation or what its purpose even is.
def DOUBLE_DIVIDE_CHECK(x):
    if x < 0:
        return x - 0.000001
    else:
        return x + 0.000001

def test_candidate_kf(dict_list, current_frame_index, frame_count_so_far):
    previous_frame_dict = dict_list[current_frame_index - 1]
    current_frame_dict = dict_list[current_frame_index]
    future_frame_dict = dict_list[current_frame_index + 1]
    
    p = previous_frame_dict
    c = current_frame_dict
    f = future_frame_dict
    
    BOOST_FACTOR = 12.5
    
    # For more documentation on the below, see https://aomedia.googlesource.com/aom/+/8ac928be918de0d502b7b492708d57ad4d817676/av1/encoder/pass2_strategy.c#1897
    MIN_INTRA_LEVEL = 0.25
    INTRA_VS_INTER_THRESH = 2.0
    VERY_LOW_INTER_THRESH = 0.05
    KF_II_ERR_THRESHOLD = 2.5
    ERR_CHANGE_THRESHOLD = 0.4
    II_IMPROVEMENT_THRESHOLD = 3.5
    KF_II_MAX = 128.0
    
    qmode = True
    #todo: allow user to set whether we're testing for constant-q mode keyframe placement or not. it's not a big difference.
    
    is_keyframe = 0
    
    pcnt_intra = 1.0 - c['pcnt_inter']
    modified_pcnt_inter = c['pcnt_inter'] - c['pcnt_neutral']
    
    second_ref_usage_thresh = get_second_ref_usage_thresh(frame_count_so_far)
    
    if ((qmode == False) or (frame_count_so_far > 2)) and (c['pcnt_second_ref'] < second_ref_usage_thresh) and (f['pcnt_second_ref'] < second_ref_usage_thresh) and ((c['pcnt_inter'] < VERY_LOW_INTER_THRESH) or ((pcnt_intra > MIN_INTRA_LEVEL) and (pcnt_intra > (INTRA_VS_INTER_THRESH * modified_pcnt_inter)) and ((c['intra_error'] / DOUBLE_DIVIDE_CHECK(c['coded_error'])) < KF_II_ERR_THRESHOLD) and ((abs(p['coded_error'] - c['coded_error']) / DOUBLE_DIVIDE_CHECK(c['coded_error']) > ERR_CHANGE_THRESHOLD) or (abs(p['intra_error'] - c['intra_error']) / DOUBLE_DIVIDE_CHECK(c['intra_error']) > ERR_CHANGE_THRESHOLD) or ((f['intra_error'] / DOUBLE_DIVIDE_CHECK(f['coded_error'])) > II_IMPROVEMENT_THRESHOLD)))):
        boost_score = 0.0
        old_boost_score = 0.0
        decay_accumulator = 1.0
        for i in range(0, 16):
            lnf = dict_list[current_frame_index + 1 + i]
            next_iiratio = (BOOST_FACTOR * lnf['intra_error'] / DOUBLE_DIVIDE_CHECK(lnf['coded_error']))
            if (next_iiratio > KF_II_MAX):
                next_iiratio = KF_II_MAX
                
            #Cumulative effect of decay in prediction quality.
            if (lnf['pcnt_inter'] > 0.85):
                decay_accumulator = decay_accumulator * lnf['pcnt_inter']
            else:
                decay_accumulator = decay_accumulator * ((0.85 + lnf['pcnt_inter']) / 2.0)
                
            #Keep a running total.
            boost_score += (decay_accumulator * next_iiratio)
            
            #Test various breakout clauses.
            if ((lnf['pcnt_inter'] < 0.05) or (next_iiratio < 1.5) or (((lnf['pcnt_inter'] - lnf['pcnt_neutral']) < 0.20) and (next_iiratio < 3.0)) or ((boost_score - old_boost_score) < 3.0) or (lnf['intra_error'] < 200)):
                break
            old_boost_score = boost_score
            
        #If there is tolerable prediction for at least the next 3 frames then break out else discard this potential key frame and move on
        if (boost_score > 30.0 and (i > 3)):
            is_keyframe = 1
    return is_keyframe

#I don't know what data format you want as output
keyframes_list = ['0']
is_keyframe_list = ['1']

if len(sys.argv) > 1:
    filename = sys.argv[1]
else:
    filename = 'stats.bin'

number_of_frames = round(os.stat(filename).st_size / 208) - 1
dict_list = []

with open(filename, 'rb') as file:
    frameBuf = file.read(208)
    while len(frameBuf) > 0:
        stats = struct.unpack('d' * 26, frameBuf)
        p = dict(zip(fields, stats))
        dict_list.append(p)
        frameBuf = file.read(208)

#intentionally skipping 0th frame and last 16 frames
frame_count_so_far = 1
for i in range(1, number_of_frames - 16):
    is_keyframe = test_candidate_kf(dict_list, i, frame_count_so_far)
    if is_keyframe == 1:
        keyframes_list.append(str(i))
        is_keyframe_list.append(str('1'))
        frame_count_so_far = 0
    else:
        is_keyframe_list.append(str('0'))
    frame_count_so_far += 1

print(keyframes_list)