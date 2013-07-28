import re, json
from annoying.functions import get_object_or_None

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.http import HttpResponse
from django.utils import simplejson
from django.db.models import Q

import settings
from config.models import Settings
from main import topicdata
from main.models import FacilityUser, VideoLog, ExerciseLog, VideoFile
from securesync.models import FacilityGroup
from updates.models import UpdateProgressLog
from utils.decorators import require_admin
from utils.general import break_into_chunks
from utils.internet import JsonResponse
from utils.jobs import force_job, job_status
from utils.orderedset import OrderedSet
from utils.videos import delete_downloaded_files
from utils.django_utils import call_command_async


def save_video_log(request):
    """
    API endpoint called by the browser (in the background), 
    while a user is viewing a video.
    """
    if "facility_user" not in request.session:
        return JsonResponse({})
    data = simplejson.loads(request.raw_post_data or "{}")
    videolog = VideoLog()
    videolog.user = request.session["facility_user"]
    videolog.youtube_id = data.get("youtube_id", "")
    old_videolog = videolog.get_existing_instance() or VideoLog()
    videolog.total_seconds_watched = old_videolog.total_seconds_watched + data.get("seconds_watched", 0)
    videolog.points = max(old_videolog.points or 0, data.get("points", 0)) or 0
    try:
        videolog.full_clean()
        videolog.save()
        return JsonResponse({
            "points": videolog.points,
            "complete": videolog.complete,
        })
    except ValidationError as e:
        return JsonResponse({"error": "Could not save VideoLog: %s" % e}, status=500)


def save_exercise_log(request):
    """
    API endpoint called by the browser, 
    while a user submits an answer to a question.
    """
    if "facility_user" not in request.session:
        return JsonResponse({})
    data = simplejson.loads(request.raw_post_data or "{}")
    exerciselog = ExerciseLog()
    exerciselog.user = request.session["facility_user"]
    exerciselog.exercise_id = data.get("exercise_id", "")
    old_exerciselog = exerciselog.get_existing_instance() or ExerciseLog()
    exerciselog.attempts = old_exerciselog.attempts + 1
    exerciselog.streak_progress = data.get("streak_progress", None)
    exerciselog.points = data.get("points", None)

    try:
        exerciselog.full_clean()
        exerciselog.save()
        return JsonResponse({})
    except ValidationError as e:
        return JsonResponse({"error": "Could not save ExerciseLog: %s" % e}, status=500)


def get_video_logs(request):
    """
    API endpoint called on page-load for topic pages containing videos;
    returns video progress for all videos on that page.
    """
    data = simplejson.loads(request.raw_post_data or "[]")
    if not isinstance(data, list):
        return JsonResponse([])
    if "facility_user" not in request.session:
        return JsonResponse([])
    user = request.session["facility_user"]
    responses = []
    for youtube_id in data:
        response = _get_video_log_dict(request, user, youtube_id)
        if response:
            responses.append(response)
    return JsonResponse(responses)


def _get_video_log_dict(request, user, youtube_id):
    """
    Utility that converts a video log to a dictionary
    """
    if not youtube_id:
        return {}
    try:
        videolog = VideoLog.objects.filter(user=user, youtube_id=youtube_id).latest("counter")
    except VideoLog.DoesNotExist:
        return {}
    return {
        "youtube_id": youtube_id,
        "total_seconds_watched": videolog.total_seconds_watched,
        "complete": videolog.complete,
        "points": videolog.points,
    }


def get_exercise_logs(request):
    """
    API endpoint called on page-load for topic pages containing exercises;
    returns exercise progress for all exercises on that page.
    """
    data = simplejson.loads(request.raw_post_data or "[]")
    if not isinstance(data, list):
        return JsonResponse([])
    if "facility_user" not in request.session:
        return JsonResponse([])
    user = request.session["facility_user"]
    responses = []
    for exercise_id in data:
        response = _get_exercise_log_dict(request, user, exercise_id)
        if response:
            responses.append(response)
    return JsonResponse(responses)


def _get_exercise_log_dict(request, user, exercise_id):
    """
    Utility that converts a video log to a dictionary
    """
    if not exercise_id:
        return {}
    try:
        exerciselog = ExerciseLog.objects.get(user=user, exercise_id=exercise_id)
    except ExerciseLog.DoesNotExist:
        return {}
    return {
        "exercise_id": exercise_id,
        "streak_progress": exerciselog.streak_progress,
        "complete": exerciselog.complete,
        "points": exerciselog.points,
        "struggling": exerciselog.struggling,
    }

@require_admin
def start_video_download(request):
    """
    API endpoint for launching the videodownload job.
    """
    youtube_ids = OrderedSet(simplejson.loads(request.raw_post_data or "{}").get("youtube_ids", []))

    video_files_to_create = [id for id in youtube_ids if not get_object_or_None(VideoFile, youtube_id=id)]
    video_files_to_update = youtube_ids - OrderedSet(video_files_to_create)

    VideoFile.objects.bulk_create([VideoFile(youtube_id=id, flagged_for_download=True) for id in video_files_to_create])

    for chunk in break_into_chunks(youtube_ids):
        video_files_needing_model_update = VideoFile.objects.filter(download_in_progress=False, youtube_id__in=chunk).exclude(percent_complete=100)
        video_files_needing_model_update.update(percent_complete=0, cancel_download=False, flagged_for_download=True)

    force_job("videodownload", "Download Videos")
    return JsonResponse({})

@require_admin
def delete_videos(request):
    """
    API endpoint for deleting videos.
    """
    youtube_ids = simplejson.loads(request.raw_post_data or "{}").get("youtube_ids", [])
    for id in youtube_ids:
        # We should mark all metadata first, to avoid conflicts
        #   across processes (not currently an issue, but could be)
        videofile = get_object_or_None(VideoFile, youtube_id=id)
        if not videofile:
            continue
        videofile.cancel_download = True
        videofile.flagged_for_download = False
        videofile.flagged_for_subtitle_download = False
        videofile.save()

        # Now nobody should be touching this file.
        delete_downloaded_files(id)
    return JsonResponse({})


@require_admin
def cancel_video_download(request):

    # clear all download in progress flags, to make sure new downloads will go through
    VideoFile.objects.all().update(download_in_progress=False)

    # unflag all video downloads
    VideoFile.objects.filter(flagged_for_download=True).update(cancel_download=True, flagged_for_download=False)

    force_job("videodownload", stop=True)
    log = UpdateProgressLog.get_active_log(process_name="videodownload", create_new=False)
    if log:
        log.cancel_progress()

    return JsonResponse({})



# Functions below here focused on topic tree (used for dynatree js code)
def annotate_topic_tree(node, level=0, statusdict=None):
    if not statusdict:
        statusdict = {}
    if node["kind"] == "Topic":
        if "Video" not in node["contains"]:
            return None
        children = []
        unstarted = True
        complete = True
        for child_node in node["children"]:
            child = annotate_topic_tree(child_node, level=level+1, statusdict=statusdict)
            if child:
                if child["addClass"] == "unstarted":
                    complete = False
                if child["addClass"] == "partial":
                    complete = False
                    unstarted = False
                if child["addClass"] == "complete":
                    unstarted = False
                children.append(child)
        return {
            "title": node["title"],
            "tooltip": re.sub(r'<[^>]*?>', '', node["description"] or ""),
            "isFolder": True,
            "key": node["slug"],
            "children": children,
            "addClass": complete and "complete" or unstarted and "unstarted" or "partial",
            "expand": level < 1,
        }
    if node["kind"] == "Video":
        #statusdict contains an item for each video registered in the database
        # will be {} (empty dict) if there are no videos downloaded yet
        percent = statusdict.get(node["youtube_id"], 0)
        if not percent:
            status = "unstarted"
        elif percent == 100:
            status = "complete"
        else:
            status = "partial"
        return {
            "title": node["title"],
            "tooltip": re.sub(r'<[^>]*?>', '', node["description"] or ""),
            "key": node["youtube_id"],
            "addClass": status,
        }
    return None

@require_admin
def get_annotated_topic_tree(request):
    statusdict = dict(VideoFile.objects.values_list("youtube_id", "percent_complete"))
    att = annotate_topic_tree(topicdata.TOPICS, statusdict=statusdict)
    return JsonResponse(att)


"""
Subtitles
"""
@require_admin
def start_subtitle_download(request):
    new_only = simplejson.loads(request.raw_post_data or "{}").get("new_only", False)
    language = simplejson.loads(request.raw_post_data or "{}").get("language", "")
    language_list = topicdata.LANGUAGE_LIST
    current_language = Settings.get("subtitle_language")
    new_only = new_only and (current_language == language)
    if language in language_list:
        Settings.set("subtitle_language", language)
    else:
        return JsonResponse({"error": "This language is not currently supported - please update the language list"}, status=500)
    if new_only:
        videofiles = VideoFile.objects.filter(Q(percent_complete=100) | Q(flagged_for_download=True), subtitles_downloaded=False)
    else:
        videofiles = VideoFile.objects.filter(Q(percent_complete=100) | Q(flagged_for_download=True))
    for videofile in videofiles:
        videofile.cancel_download = False
        if videofile.subtitle_download_in_progress:
            continue
        videofile.flagged_for_subtitle_download = True
        if not new_only:
            videofile.subtitles_downloaded = False
        videofile.save()
    force_job("subtitledownload", "Download Subtitles")
    return JsonResponse({})

@require_admin
def check_subtitle_download(request):
    videofiles = VideoFile.objects.filter(flagged_for_subtitle_download=True)
    return JsonResponse(videofiles.count())

@require_admin
def get_subtitle_download_list(request):
    videofiles = VideoFile.objects.filter(flagged_for_subtitle_download=True).values("youtube_id")
    video_ids = [video["youtube_id"] for video in videofiles]
    return JsonResponse(video_ids)

@require_admin
def cancel_subtitle_download(request):
    # unflag all subtitle downloads
    VideoFile.objects.filter(flagged_for_subtitle_download=True).update(cancel_download=True, flagged_for_subtitle_download=False)

    force_job("subtitledownload", stop=True)
    return JsonResponse({})


"""
Software updates
"""

@require_admin
def start_update_kalite(request):
    data = json.loads(request.raw_post_data)

    if request.META.get("CONTENT_TYPE", "") == "application/json" and "url" in data:
        # Got a download url
        call_command_async("update", url=data["url"])

    elif request.META.get("CONTENT_TYPE", "") == "application/zip":
        # Streamed a file; save and call
        fp, tempfile = tempfile.mkstmp()
        with fp:
            write(request.content)
        call_command_async("update", zip_file=tempfile)

    return JsonResponse({})

@require_admin
def check_update_kalite(request):
    videofiles = VideoFile.objects.filter(flagged_for_subtitle_download=True)
    return JsonResponse(videofiles.count())

@require_admin
def get_update_kalite_list(request):
    videofiles = VideoFile.objects.filter(flagged_for_subtitle_download=True).values("youtube_id")
    video_ids = [video["youtube_id"] for video in videofiles]
    return JsonResponse(video_ids)

@require_admin
def cancel_update_kalite(request):
    # unflag all subtitle downloads
    VideoFile.objects.filter(flagged_for_subtitle_download=True).update(cancel_download=True, flagged_for_subtitle_download=False)

    force_job("subtitledownload", stop=True)
    return JsonResponse({})


# Functions below here focused on users

@require_admin
def remove_from_group(request):
    """
    API endpoint for removing users from group
    (from user management page)
    """
    users = simplejson.loads(request.raw_post_data or "{}").get("users", "")
    users_to_remove = FacilityUser.objects.filter(username__in=users)
    users_to_remove.update(group=None)
    return JsonResponse({})

@require_admin
def move_to_group(request):
    users = simplejson.loads(request.raw_post_data or "{}").get("users", [])
    group = simplejson.loads(request.raw_post_data or "{}").get("group", "")
    group_update = FacilityGroup.objects.get(pk=group)
    users_to_move = FacilityUser.objects.filter(username__in=users)
    users_to_move.update(group=group_update)
    return JsonResponse({})

@require_admin
def delete_users(request):
    users = simplejson.loads(request.raw_post_data or "{}").get("users", [])
    users_to_delete = FacilityUser.objects.filter(username__in=users)
    users_to_delete.delete()
    return JsonResponse({})

