import json
import logging
import sys
import traceback
from pathlib import Path
from typing import List
from xml.etree import ElementTree

from PyQt5.QtCore import QFile, QIODevice
from PyQt5.QtXml import QDomDocument
from qfieldcloud.qgis.utils import (
    BaseException,
    Step,
    get_layer_filename,
    has_ping,
    is_localhost,
    logger_context,
)
from qgis.core import QgsMapRendererParallelJob, QgsMapSettings, QgsProject
from qgis.gui import QgsLayerTreeMapCanvasBridge, QgsMapCanvas
from qgis.PyQt.QtCore import QEventLoop, QSize
from qgis.PyQt.QtGui import QColor
from qgis.testing import start_app

logging.basicConfig(
    stream=sys.stderr, level=logging.DEBUG, format="%(asctime)s %(levelname)s %(msg)s"
)


class ProjectFileNotFoundException(BaseException):
    message = 'Project file "%(project_filename)s" does not exist'


class InvalidFileExtensionException(BaseException):
    message = (
        'Project file "%(project_filename)s" has unknown file extension "%(extension)s"'
    )


class InvalidXmlFileException(BaseException):
    message = (
        'Project file "%(project_filename)s" is an invalid XML document:\n%(xml_error)s'
    )


class InvalidQgisFileException(BaseException):
    message = 'Project file "%(project_filename)s" is invalid QGIS file:\n%(error)s'


class InvalidLayersException(BaseException):
    message = 'Project file "%(project_filename)s" contains invalid layers'


class FailedThumbnailGenerationException(BaseException):
    message = "Failed to generate project thumbnail:\n%(reason)s"


def check_valid_project_file(project_filename: Path) -> None:
    logging.info("Check QGIS project file validity...")

    if not project_filename.exists():
        raise ProjectFileNotFoundException(project_filename=project_filename)

    if project_filename.suffix == ".qgs":
        try:
            with open(project_filename) as f:
                ElementTree.fromstring(f.read())
        except ElementTree.ParseError as err:
            raise InvalidXmlFileException(
                project_filename=project_filename, xml_error=err
            )
    elif project_filename.suffix != ".qgz":
        raise InvalidFileExtensionException(
            project_filename=project_filename, extension=project_filename.suffix
        )


def load_project_file(project_filename: Path) -> QgsProject:
    logging.info("Open QGIS project file...")

    start_app()

    project = QgsProject.instance()
    if not project.read(str(project_filename)):
        raise InvalidXmlFileException(
            project_filename=project_filename, error=project.error()
        )

    return project


def check_layer_validity(project: QgsProject) -> List:
    logging.info("Check layer and datasource validity...")

    has_invalid_layers = False
    layers_summary = []

    for layer in project.mapLayers().values():
        error = layer.error()
        layer_data = {
            "id": layer.name(),
            "name": layer.name(),
            "is_valid": layer.isValid(),
            "datasource": layer.dataProvider().uri().uri(),
            "error_summary": error.summary() if error.messageList() else "",
            "error_message": layer.error().message(),
            "filename": get_layer_filename(layer),
            "provider_error_summary": None,
            "provider_error_message": None,
        }
        layers_summary.append(layer_data)

        if layer_data["is_valid"]:
            continue

        has_invalid_layers = True
        data_provider = layer.dataProvider()

        if data_provider:
            data_provider_error = data_provider.error()

            layer_data["provider_error_summary"] = (
                data_provider_error.summary()
                if data_provider_error.messageList()
                else ""
            )
            layer_data["provider_error_message"] = data_provider_error.message()

            if not layer_data["provider_error_summary"]:
                service = data_provider.uri().service()
                if service:
                    layer_data[
                        "provider_error_summary"
                    ] = f'Unable to connect to service "{service}"'

                host = data_provider.uri().host()
                port = (
                    int(data_provider.uri().port())
                    if data_provider.uri().port()
                    else None
                )
                if host and (is_localhost(host, port) or has_ping(host)):
                    layer_data[
                        "provider_error_summary"
                    ] = f'Unable to connect to host "{host}"'

        else:
            layer_data["provider_error_summary"] = "No data provider available"

    if has_invalid_layers:
        raise InvalidLayersException(layers_summary=layers_summary)

    return layers_summary


def generate_thumbnail(project: QgsProject, thumbnail_filename: Path) -> None:
    """Create a thumbnail for the project

    As from https://docs.qgis.org/3.16/en/docs/pyqgis_developer_cookbook/composer.html#simple-rendering

    Args:
        project (QgsProject)
        thumbnail_filename (Path)
    """
    logging.info("Generate project thumbnail image...")

    layer_tree = project.layerTreeRoot()
    canvas = QgsMapCanvas()
    QgsLayerTreeMapCanvasBridge(layer_tree, canvas)

    doc = QDomDocument("qgis")
    file = QFile(project.fileName())
    if file.open(QIODevice.ReadOnly):
        (_retval, error, _error_line, _error_column) = doc.setContent(file, False)
        if error:
            raise InvalidXmlFileException(
                project_filename=project_filename, error=error
            )

    canvas.readProject(doc)
    settings = QgsMapSettings()
    settings.setLayers(reversed(list(layer_tree.customLayerOrder())))
    settings.setBackgroundColor(QColor(255, 255, 255))
    settings.setOutputSize(QSize(250, 250))
    settings.setDestinationCrs(project.crs())
    settings.setExtent(canvas.extent())

    render = QgsMapRendererParallelJob(settings)

    def finished():
        if not render.renderedImage().save(str(thumbnail_filename)):
            logging.info("Failed to create project thumbnail image")

    render.finished.connect(finished)

    render.start()

    loop = QEventLoop()
    render.finished.connect(loop.quit)
    loop.exec_()

    if not Path(thumbnail_filename).exists():
        raise FailedThumbnailGenerationException(
            project_filename=project_filename, reason="File does not exist."
        )


def process_projectfile(
    project_filename: Path,
    thumbnail_filename: Path,
    feedback_filename: Path = None,
) -> None:
    feedback = {}
    # argument values by name. Note it may be modified after the successful completion of each step.
    arg_values = {
        "project_filename": project_filename,
        "thumbnail_filename": thumbnail_filename,
    }
    steps: List[Step] = [
        Step(
            name="Project Validity Check",
            arg_names=["project_filename"],
            method=check_valid_project_file,
        ),
        Step(
            name="Opening Check",
            arg_names=["project_filename"],
            method=load_project_file,
            return_names=["project"],
            public_returns=["project"],
        ),
        Step(
            name="Layer Validity Check",
            arg_names=["project"],
            method=check_layer_validity,
            return_names=["layers_summary"],
            output_names=["layers_summary"],
        ),
        Step(
            name="Generate Thumbnail Image",
            arg_names=["project", "thumbnail_filename"],
            method=generate_thumbnail,
        ),
    ]

    try:
        for step in steps:
            with logger_context(step):
                args = [arg_values[arg_name] for arg_name in step.arg_names]
                return_values = step.method(*args)
                return_values = (
                    return_values if len(step.return_names) > 1 else (return_values,)
                )

                return_map = {}
                for name, value in zip(step.return_names, return_values):
                    return_map[name] = value

                for output_name in step.output_names:
                    step.outputs[output_name] = return_map[output_name]

                for return_name in step.public_returns:
                    arg_values[return_name] = return_map[return_name]

    except BaseException as err:
        feedback["error"] = str(err)
        (_type, _value, tb) = sys.exc_info()
        feedback["error_stack"] = traceback.format_tb(tb)
    finally:
        feedback["steps"] = [
            {
                "name": step.name,
                "stage": step.stage,
                "outputs": step.outputs,
            }
            for step in steps
        ]

        if feedback_filename:
            with open(feedback_filename, "w") as f:
                json.dump(feedback, f, indent=2, sort_keys=True)
        else:
            print("Feedback:")
            print(json.dumps(feedback, indent=2, sort_keys=True))


if __name__ == "__main__":
    import sys

    project_filename = Path(sys.argv[1])

    process_projectfile(project_filename, Path("/tmp/thumbnail.png"))
