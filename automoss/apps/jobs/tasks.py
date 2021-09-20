
from ..results.models import MOSSResult, Match
from .models import Job, Submission, JobEvent
from django.utils.timezone import now
from django.core.files.uploadedfile import UploadedFile
from ..moss.pinger import Pinger, LoadStatus
from ..moss.moss import (
    MOSS,
    Result,
    RecoverableMossException,
    EmptyResponse,
    FatalMossException,
    is_valid_moss_url
)
from ...settings import (
    SUPPORTED_LANGUAGES,
    PROCESSING_STATUS,
    UPLOADING_STATUS,
    PARSING_STATUS,
    INQUEUE_STATUS,
    COMPLETED_STATUS,
    FAILED_STATUS,
    SUBMISSION_TYPES,
    FILES_NAME,
    JOB_UPLOAD_TEMPLATE,
    DEBUG,

    MIN_RETRY_TIME,
    MAX_RETRY_TIME,
    MAX_RETRY_DURATION,
    EXPONENTIAL_BACKOFF_BASE_RANGE,
    FIRST_RETRY_INSTANT,

    # Events
    INQUEUE_EVENT,
    UPLOADING_EVENT,
    PROCESSING_EVENT,
    PARSING_EVENT,
    COMPLETED_EVENT,
    FAILED_EVENT,
    RETRY_EVENT,
    ERROR_EVENT
)
from ..utils.core import retry
import os
import json
import time
import socket
from celery.decorators import task
from celery.utils.log import get_task_logger
logger = get_task_logger(__name__)


@task(name='Upload')
def process_job(job_id):
    """Basic interface for generating a report from MOSS"""

    job = Job.objects.get(job_id=job_id)

    if job.status != INQUEUE_STATUS:
        # A job will only be started if it is in the queue.
        # Prevents jobs from being processed more than once.
        # Necessary because redis and celery store their own caches/lists
        # of jobs, which may cause process_job to be run more than once.
        return


    job.start_date = now()
    logger.info(f'Starting job {job_id} with status {job.status}')

    base_dir = JOB_UPLOAD_TEMPLATE.format(job_id=job.job_id)


    paths = {}

    for file_type in SUBMISSION_TYPES:
        path = os.path.join(base_dir, file_type)
        if not os.path.isdir(path):
            continue  # Ignore if none of these files were submitted

        paths[file_type] = []
        for f in os.listdir(path):
            file_path = os.path.join(path, f)
            if os.path.getsize(file_path) > 0:
                # Only add non-empty files
                paths[file_type].append(file_path)

    if not paths.get(FILES_NAME):
        job.status = FAILED_STATUS
        job.save()

        JobEvent.objects.create(job=job, type=FAILED_EVENT, message='No files supplied')
        return None

    num_attempts = 0
    url = None
    result = None

    for attempt, time_to_sleep in retry(MIN_RETRY_TIME, MAX_RETRY_TIME, EXPONENTIAL_BACKOFF_BASE_RANGE, MAX_RETRY_DURATION, FIRST_RETRY_INSTANT):
        num_attempts = attempt

        try:
            error = None
            if not is_valid_moss_url(url):
                job.status = UPLOADING_STATUS
                job.save()
                # Keep retrying until valid url has been generated
                # Do not restart whole job if this succeeds but parsing fails

                def on_processing_start():
                    job.status = PROCESSING_STATUS
                    job.save()
                    JobEvent.objects.create(job=job, type=PROCESSING_EVENT, message='Started generating similarity report')

                url = MOSS.generate_url(
                    user_id=job.user.moss_id,
                    language=SUPPORTED_LANGUAGES[job.language][1],
                    **paths,
                    max_until_ignored=job.max_until_ignored,
                    max_displayed_matches=job.max_displayed_matches,
                    use_basename=True,

                    # TODO other events to log?
                    # on_start=None,
                    # on_connect=None,

                    on_upload_start=lambda: JobEvent.objects.create(job=job, type=UPLOADING_EVENT, message='Started uploading files to MOSS'),
                    on_upload_finish=lambda: JobEvent.objects.create(job=job, type=UPLOADING_EVENT, message='Finished uploading'),

                    on_processing_start=on_processing_start,
                    on_processing_finish=lambda: JobEvent.objects.create(job=job, type=PROCESSING_EVENT, message='Finished processing'),
                )
                msg = f'Generated url: "{url}"'
                logger.info(msg)

            msg = 'Start parsing report'
            logger.info(msg)

            job.status = PARSING_STATUS
            job.save()
            JobEvent.objects.create(job=job, type=PARSING_EVENT, message=msg)

            # Parsing and extraction
            result = MOSS.generate_report(url)
            msg = f'Result finished parsing: {len(result.matches)} matches detected.'
            logger.info(msg)
            JobEvent.objects.create(job=job, type=PARSING_EVENT, message=msg)

            break  # Success, do not retry

        except (RecoverableMossException, socket.error) as e:
            error = e  # Handled below

        except EmptyResponse as e:
            # Job ended after
            error = e

            load_status, ping, average_ping = Pinger.determine_load()
            ping_message = f'({ping} vs. {average_ping})'

            if load_status == LoadStatus.NORMAL:
                msg = f'Moss is not under load {ping_message} - job ({job_id}) will never finish'
                logger.debug(msg)
                break

            elif load_status == LoadStatus.UNDER_LOAD:
                msg = f'Moss is under load {ping_message}, retrying job ({job_id})'
            else:
                msg = f'Moss is down {ping_message}, retrying job ({job_id})'
            
            logger.debug(msg)

        except FatalMossException as e:
            break  # Will be handled below (result is None)

        except Exception as e:
            # TODO something catastrophic happened
            # Do some logging here
            logger.error(f'Unknown error: {e}')
            break  # Will be handled below (result is None)

        
        msg = f'(Attempt {attempt}) Error: {error} | Retrying in {time_to_sleep} seconds'

        # We can retry
        logger.warning(msg)
        JobEvent.objects.create(job=job, type=RETRY_EVENT, message=msg)

        time.sleep(time_to_sleep)

    failed = result is None

    # Represents when no more processing of the job will occur
    job.completion_date = now()

    try:
        if failed:
            job.status = FAILED_STATUS
            JobEvent.objects.create(job=job, type=FAILED_EVENT)
            return None

        # Parse result
        moss_result = MOSSResult.objects.create(
            job=job,
            url=result.url
        )

        for match in result.matches:
            first_submission = Submission.objects.filter(
                job=job, submission_id=match.name_1).first()
            second_submission = Submission.objects.filter(
                job=job, submission_id=match.name_2).first()

            # Ensure matching submission is found (avoid future errors)
            if first_submission and second_submission:
                Match.objects.create(
                    moss_result=moss_result,
                    first_submission=first_submission,
                    second_submission=second_submission,
                    first_percentage=match.percentage_1,
                    second_percentage=match.percentage_2,
                    lines_matched=match.lines_matched,
                    line_matches=match.line_matches
                )

        JobEvent.objects.create(job=job, type=COMPLETED_EVENT)
        job.status = COMPLETED_STATUS
        return result.url

    finally:
        job.save()

        if DEBUG:
            # Calculate average file_size
            num_files = len(paths[FILES_NAME])
            avg_file_size = sum([os.path.getsize(x)
                                for x in paths[FILES_NAME]])/num_files

            log_info = vars(job).copy()
            log_info.pop('_state', None)
            log_info.update({
                'num_files': num_files,
                'avg_file_size': avg_file_size,
                'moss_id': job.user.moss_id,
                'num_attempts': num_attempts
            })

            log_info.update(Pinger.ping() or {})
            logger.debug(f'Job info: {log_info}')

            with open('jobs.log', 'a+') as fp:
                log_info['duration'] = (
                    log_info['completion_date'] - log_info['start_date']).total_seconds()
                json.dump(log_info, fp, sort_keys=True, default=str)
                print(file=fp)
