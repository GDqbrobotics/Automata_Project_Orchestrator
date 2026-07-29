This document is a description of the project context and the role of the GUI within it.

## What is AUTOMATA project

AUTOMATA is a project financiated by the European Union that involves various companies and universities, the aim is to create a machine that automatize the analysis of archeological objects. This machine will be used by museums, universities and research institutions to create a database of digital twins for pottery pieces and lithics.
The operator will load the machine with a gridded box, where the samples are placed, and the system will perform 3D Imaging (3DI), Hyper Spectral Imaging (HSI), uXRF measures and Raman measures. Then all this data will be automatically sent to a database in the cloud.

## Design of the AUTOMATA machine

Going a little bit further in the physical structure of the machine, it is composed  by an area where the box will be placed (with a camera); then a first robotic arm of Universal Robot with a costumized end effector for pick-and-place 3DI and HSI; then a rotating platform with 2 transparent disks; then a second robotic arm with a costumized end effector for integrating the XRF and Raman analyzers.

## The workflow is the following:

1. Human operator loads the gridded box with samples into the machine.
2. Vision system identify the objects, create IDs, create analyzing order.
3. Box loop starts. 
4. First Robot performs tool change to mount the gripper (if is not on mounted) and picks up the target object.
5. The object is placed on the transparent disk in first position.
6. The First Robot change end effector and performs 3DI and HSI on the object.
7. An AI system uses that data in order to extrapolate interest points on the object for next puntual measurements.
8. The platform rotated and the disk with the object arrives in the second position.
9. Second Robot performs XRF measurements on the choosen points.
10. Second Robot performs Raman measurements on the choosen points.
11. Platform rotates again taking the disk with the object back in first position.
12. First Robot after mounting the gripper, picks the object from disk and put it back in its original place inside box.
13. Repeat untill all object in the box have been analyzed.

This workflow allows to cut time because first robot performs steps *12-4-5-6* in parallel with second robot performing steps *9-10*.

During the workflow the human operator can insert alias IDs from the GUI to the objects in the box, this IDs will be used for on the database in cloud (the machine is already working in the meanwhile with the automatically assigned IDs).
After storage database IDs are confirmed, the data will be sent to the cloud for each object completed.

## Software Architecture

The System is distribuited on different computers (Ubuntu and Windows involved): all the sub-systems communicate with a GUI orchestrator (the code of this repo) through MQTT.

The sub-systems are:

| Subsystem | Hardware Involved | Description | Software Framework
|---|---|---|---|
| GUI orchestrator | Ubuntu PC, Touch screen | central GUI application, workflow orchestration, MQTT messaging | NodeJs |
| Vision system | RGB-D camera, gridded box, lighting | object detection, ID creation, analysis order generation | Docker, Python application | 
| First robotic arm | Universal Robot arm, custom end effector, gripper | pick-and-place, 3DI/HSI object handling, tool-change control | Docker, ROS2 nodes |
| Rotating platform | motorized rotary stage, transparent disks | object indexing between measurement positions | Digital I/Os interface |
| Second robotic arm | Universal Robot arm, custom end effector | XRF/Raman measurement positioning and execution | Docker, ROS2 nodes |
| XRF interface | XRF analyzer hardware | manage communication, trigger acquisition, and transfer data from XRF device | ? |
| Raman interface | Raman analyzer hardware | manage communication, trigger acquisition, and transfer data from Raman device | ? |
| 3DI/HSI interface | 3DI and HSI imaging modules | manage communication, synchronization, and data acquisition from 3DI/HSI devices | ? |
| AI analysis | local compute server | interest point extraction, measurement planning for XRF/Raman | ? |
| Cloud database interface | network connection, cloud servers | data upload, ID confirmation, storage of object records | ? |

## GUI design principles

The GUI orchestrate and interface the system with operator, it follows an interaction hierarchy based on State Machine design. 

From highest to lowest level:

1. **Human Operator** using the Graphical Interface directly acts on **System State Machine** ( Start, Stop, Pause, Perform Required Action).
2. **System State Machine** acts and reacts on **Sub-systems State Machines**.
3. **Sub-systems State Machines** are multiple entities, each one reads and sends data to the actual sub-system via MQTT communication. 

All the state machines are defined by a configuration yaml file. The rules for this configuration files are described in the file [docs/STATE_MACHINE.md](STATE_MACHINE.md)
