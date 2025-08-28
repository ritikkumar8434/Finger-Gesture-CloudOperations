"""
gesture_aws_ops.py

AI-Powered Cloud Operations via Finger Gesture Recognition
 - Uses MediaPipe for hand/finger detection
 - Maps simple finger-count gestures to a small set of AWS operations via boto3
 - Requires explicit console confirmation before any state-changing AWS call

Author: (You)
Notes:
 - Test only in a dev/sandbox AWS account.
 - Configure AWS credentials via environment or ~/.aws/credentials before running.
"""

import time
import os
import threading
import boto3
import cv2
import mediapipe as mp
import numpy as np

# ---------------------------
# Configuration
# ---------------------------

DEBOUNCE_FRAMES = 8        # gesture must persist for this many frames to trigger
COOLDOWN_SECONDS = 4       # seconds to wait after triggering before accepting new gestures

# AWS clients (will use default session/credentials resolution)
ec2 = boto3.client("ec2")
s3 = boto3.client("s3")

# ---------------------------
# MediaPipe helper functions
# ---------------------------

mp_hands = mp.solutions.hands
mp_drawing = mp.solutions.drawing_utils

def count_fingers(hand_landmarks, hand_label):
    """
    Count raised fingers for a single hand using landmark positions.
    Returns an int 0..5

    Logic:
    - Thumb: compare TIP and IP x-coordinates (depends on left/right hand)
    - Fingers (index, middle, ring, pinky): compare TIP y < PIP y (tip above pip => extended)
    """
    # landmark indices: https://google.github.io/mediapipe/solutions/hands
    # TIPs: 4 (thumb), 8 (index), 12 (middle), 16 (ring), 20 (pinky)
    # PIPs: 3 (thumb ip), 6 (index pip), 10 (middle pip), 14 (ring pip), 18 (pinky pip)
    tips_ids = [4, 8, 12, 16, 20]
    pip_ids = [3, 6, 10, 14, 18]

    img_handed = hand_landmarks.landmark  # list of normalized landmarks

    fingers = []

    # Thumb logic (horizontal comparison)
    # For right hand, thumb tip.x < ip.x when extended (may vary by camera/mirror)
    # We'll use hand_label to decide direction
    try:
        thumb_tip = img_handed[tips_ids[0]]
        thumb_ip = img_handed[pip_ids[0]]
        if hand_label == "Right":
            fingers.append(1 if thumb_tip.x < thumb_ip.x else 0)
        else:  # Left
            fingers.append(1 if thumb_tip.x > thumb_ip.x else 0)
    except Exception:
        fingers.append(0)

    # Other four fingers: tip.y < pip.y when extended (normalized coords where 0 is top)
    for tid, pid in zip(tips_ids[1:], pip_ids[1:]):
        try:
            tip = img_handed[tid]
            pip = img_handed[pid]
            fingers.append(1 if tip.y < pip.y else 0)
        except Exception:
            fingers.append(0)

    return sum(fingers)

# ---------------------------
# AWS operation wrappers (safe - require confirmation)
# ---------------------------

def list_ec2_instances():
    """List EC2 instances (ID, state, type, name)"""
    resp = ec2.describe_instances()
    instances = []
    for res in resp.get("Reservations", []):
        for inst in res.get("Instances", []):
            iid = inst.get("InstanceId")
            state = inst.get("State", {}).get("Name")
            itype = inst.get("InstanceType")
            # Find Name tag if present
            name = None
            for tag in inst.get("Tags", []) if inst.get("Tags") else []:
                if tag.get("Key") == "Name":
                    name = tag.get("Value")
            instances.append({"InstanceId": iid, "State": state, "Type": itype, "Name": name})
    print("\n[EC2 Instances]")
    for i in instances:
        print(f"- {i['InstanceId']} | state={i['State']} | type={i['Type']} | name={i['Name']}")
    return instances

def start_ec2_instance(instance_id):
    print(f"About to start EC2 instance: {instance_id}")
    confirm = input("Type 'yes' to confirm START: ").strip().lower()
    if confirm == "yes":
        resp = ec2.start_instances(InstanceIds=[instance_id])
        print("Start request sent. Result:", resp)
    else:
        print("Start cancelled.")

def stop_ec2_instance(instance_id):
    print(f"About to stop EC2 instance: {instance_id}")
    confirm = input("Type 'yes' to confirm STOP: ").strip().lower()
    if confirm == "yes":
        resp = ec2.stop_instances(InstanceIds=[instance_id])
        print("Stop request sent. Result:", resp)
    else:
        print("Stop cancelled.")

def list_s3_buckets():
    resp = s3.list_buckets()
    print("\n[S3 Buckets]")
    for b in resp.get("Buckets", []):
        print(f"- {b.get('Name')}")
    return resp.get("Buckets", [])

def create_s3_bucket(bucket_name, region=None):
    print(f"About to create S3 bucket: {bucket_name} (region={region})")
    confirm = input("Type 'yes' to confirm CREATE BUCKET: ").strip().lower()
    if confirm != "yes":
        print("Bucket creation cancelled.")
        return
    try:
        if region is None:
            resp = s3.create_bucket(Bucket=bucket_name)
        else:
            resp = s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region}
            )
        print("Bucket created:", resp)
    except Exception as e:
        print("Error creating bucket:", e)

# ---------------------------
# Gesture → action dispatcher
# ---------------------------

last_trigger_time = 0

def handle_gesture(count):
    """
    Map finger count to AWS action.
    This function runs in main thread (called after debounced detection).
    """
    global last_trigger_time
    now = time.time()
    if now - last_trigger_time < COOLDOWN_SECONDS:
        # in cooldown
        return
    last_trigger_time = now

    print(f"\n[Gesture detected] Finger count = {count}")
    if count == 1:
        # List EC2 instances (safe read-only)
        list_ec2_instances()
    elif count == 2:
        # Start an EC2 instance (dangerous) - ask instance id & confirm
        iid = input("Enter EC2 InstanceId to START (or blank to cancel): ").strip()
        if iid:
            start_ec2_instance(iid)
        else:
            print("Start operation cancelled by user.")
    elif count == 3:
        # Stop an EC2 instance
        iid = input("Enter EC2 InstanceId to STOP (or blank to cancel): ").strip()
        if iid:
            stop_ec2_instance(iid)
        else:
            print("Stop operation cancelled by user.")
    elif count == 4:
        list_s3_buckets()
    elif count == 5:
        bucket = input("Enter new S3 bucket name (DNS-compliant) or blank to cancel: ").strip()
        if bucket:
            # region detection
            region = boto3.session.Session().region_name
            create_s3_bucket(bucket, region=region)
        else:
            print("Bucket creation cancelled by user.")
    else:
        # 0 or other: no action
        # You can log or show indicator
        pass

# ---------------------------
# Main loop: webcam + mediapipe
# ---------------------------

def main():
    print("Starting Gesture → AWS Operator.")
    print("Make sure your AWS credentials are configured (environment or ~/.aws/credentials).")
    print("Mapping: 1=list EC2 | 2=start EC2 | 3=stop EC2 | 4=list S3 | 5=create S3")
    print("Hold gesture steady for a moment to trigger. Press 'q' to quit.")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("ERROR: Cannot open webcam.")
        return

    with mp_hands.Hands(
        static_image_mode=False,
        max_num_hands=1,
        min_detection_confidence=0.6,
        min_tracking_confidence=0.5
    ) as hands:
        frame_count = 0
        stable_count = 0
        last_count = None

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    print("Failed to grab frame.")
                    break

                # Flip for mirror effect if you prefer
                frame = cv2.flip(frame, 1)
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                results = hands.process(frame_rgb)

                finger_count = 0
                label = ""
                if results.multi_hand_landmarks:
                    # We'll take the first hand
                    for hand_landmarks, handedness in zip(results.multi_hand_landmarks, results.multi_handedness):
                        hand_label = handedness.classification[0].label  # 'Left' or 'Right'
                        finger_count = count_fingers(hand_landmarks, hand_label)
                        # draw
                        mp_drawing.draw_landmarks(frame, hand_landmarks, mp_hands.HAND_CONNECTIONS)
                        label = f"{hand_label} hand - {finger_count} fingers"
                        break

                # DISPLAY text on frame
                cv2.putText(frame, f"Fingers: {finger_count}", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2, cv2.LINE_AA)
                if label:
                    cv2.putText(frame, label, (10, 70),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2, cv2.LINE_AA)

                cv2.imshow("Gesture AWS Operator - press 'q' to quit", frame)

                # Debounce logic: require same count for N consecutive frames
                if finger_count == last_count:
                    stable_count += 1
                else:
                    stable_count = 1
                last_count = finger_count

                if stable_count >= DEBOUNCE_FRAMES and finger_count != 0:
                    # trigger in a separate thread so UI (camera display) doesn't block on user input
                    t = threading.Thread(target=handle_gesture, args=(finger_count,), daemon=True)
                    t.start()
                    stable_count = 0  # reset until next gesture

                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    print("Quitting.")
                    break

        except KeyboardInterrupt:
            print("Interrupted by user.")

        finally:
            cap.release()
            cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
