#!/bin/bash
#
# Simple script for starting the SMQTK IQR container over a directory of
# images. The ``-t`` option may be optionally provided to tile input imagery
# into 128x128 tiles (default). We drop into watching the processing status
# after starting the container.
#
# If the container is already running, we just start watching the container's
# status.
#
set -e

# Container image to use
IQR_CONTAINER=kitware/smqtk/iqr_playground
IQR_CONTAINER_VERSION="latest-cpu"
# Name for run container instance
CONTAINER_NAME="smqtk-iqr-playground-cpu"
IQR_GUI_PORT_PUBLISH=5000
IQR_REST_PORT_PUBLISH=5001

if [ -z "$( docker ps -a | grep "${CONTAINER_NAME}" 2>/dev/null )" ]
then
  IMAGE_DIR="$1"
  # Make sure image directory exists as a directory.
  if [ ! -d "${IMAGE_DIR}" ]
  then
    echo "ERROR: Input image directory path was not a directory: ${IMAGE_DIR}"
    exit 1
  fi
  shift
  docker run -d \
    -p ${IQR_GUI_PORT_PUBLISH}:5000 \
    -p ${IQR_REST_PORT_PUBLISH}:5001 \
    -v "${IMAGE_DIR}":/images \
    -v "/home/local/KHQ/josh.anderson/Projects/SMQTK/SMQTK-Classifier/smqtk_classifier":/usr/local/lib/python3.6/dist-packages/smqtk_classifier \
    -v "/home/local/KHQ/josh.anderson/Projects/SMQTK/SMQTK-IQR/smqtk_iqr":/usr/local/lib/python3.6/dist-packages/smqtk_iqr \
    -v "/home/local/KHQ/josh.anderson/Projects/SMQTK/SMQTK-Descriptors/smqtk_descriptors":/usr/local/lib/python3.6/dist-packages/smqtk_descriptors \
    --name "${CONTAINER_NAME}" \
    ${IQR_CONTAINER}:${IQR_CONTAINER_VERSION} -b "$@"
fi

watch -n1 "
docker exec ${CONTAINER_NAME} bash -c '[ -d data/image_tiles ] && echo && echo \"Image tiles generated: \$(ls data/image_tiles | wc -l)\"'
echo
docker exec ${CONTAINER_NAME} tail \
    data/logs/compute_many_descriptors.log \
    data/logs/train_itq.log data/logs/compute_hash_codes.log \
    data/logs/runApp.IqrSearchDispatcher.log \
    data/logs/runApp.IqrService.log
"
